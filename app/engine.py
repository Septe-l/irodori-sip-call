"""原稿生成 → TTS → SIP発信（baresip）"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import shutil
import tempfile
import wave
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import jpholiday

DATA = Path("/data")
OUT = DATA / "out"
OUT.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA / "config.json"

log = logging.getLogger("morning-call")

WEEK = "月火水木金土日"

DEFAULT_PROMPT = (
    "今日は{month}月{day}日（{weekday}曜日）、時刻は{time_text}です。\n"
    "あなたはモーニングコールの係です。電話越しに相手を優しく、でもしっかり起こす短い語りかけを書いてください。\n"
    "・日本語の話し言葉で、200文字程度\n"
    "・日付や曜日に軽く触れる\n"
    "・最後は「起きてくださいね」のように起床を促して締める\n"
    "・記号、絵文字、括弧、見出し、ト書きは使わない。読み上げる本文だけを出力する"
)

DEFAULTS: dict = {
    "enabled": True,
    "call_at": "08:00",
    "weekdays": [0, 1, 3, 4],          # 月=0 … 日=6
    "skip_holidays": True,
    "prepare_minutes": 4,              # 何分前から生成を始めるか
    "sip_user": "",
    "sip_password": "",
    "sip_domain": "",
    "sip_transport": "tcp",
    "stun_server": "stun.l.google.com:19302",  # stun.linphone.orgはbaresip 1.1.0で失敗する
    "medianat": "stun",                # stun / ice / none
    "codec": "opus",                   # opus（48kHz） / g711（PCMU/PCMA 8kHz、050番号などのSIPサービス向け）
    "media_enc": "srtp-mand",          # srtp-mand / srtp / none（Linphoneは暗号化必須）
    "target": "",                      # sip:xxx@sip.linphone.org
    "ring_timeout": 60,
    "llm_url": "",                    # OpenAI互換（LM Studio / Ollama など）例 http://localhost:1234
    "llm_model": "",                   # 空欄ならサーバー側の既定モデル
    "llm_token": "",
    "prompt": DEFAULT_PROMPT,
    "mcp_servers": "",                 # 原稿生成時にLLMが使えるMCPサーバー（Streamable HTTP、1行に1つ）
    "fallback_text": "おはようございます。朝ですよ。そろそろ起きましょう。",
    "tts_url": "",                    # Irodori-TTS-Server / Irodori-Autospeak 例 http://localhost:8088
    "tts_token": "",                   # IRODORI_API_KEY を設定している場合
    "voice": "",                       # 空欄ならサーバー側の既定の声
    "tts_steps": 20,
}
SECRET_KEYS = ("sip_password", "llm_token", "tts_token")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(CONFIG_PATH)


# ---------------------------------------------------------------- 原稿・音声

def build_prompt(template: str, call_at: dt.datetime) -> str:
    """{month}{day}{weekday}{time_text}は発信日時、{hour}{minute}は生成した時刻。
    知らない{...}はそのまま残す（外部から渡されたプロンプトでも落ちないように）"""
    now = dt.datetime.now()
    h, m = call_at.hour, call_at.minute
    values = {
        "month": call_at.month, "day": call_at.day, "weekday": WEEK[call_at.weekday()],
        "time_text": f"{h}時{f'{m}分' if m else 'ちょうど'}",
        "hour": now.hour, "minute": now.minute,
    }
    return re.sub(r"\{(\w+)\}", lambda mo: str(values.get(mo.group(1), mo.group(0))), template)


MCP_MAX_ROUNDS = 8          # ツール呼び出しの往復の上限
MCP_RESULT_CHARS = 8000     # ツール結果をLLMに渡す最大文字数


def mcp_urls(cfg: dict) -> list[str]:
    return [u.strip() for u in cfg.get("mcp_servers", "").splitlines() if u.strip() and not u.strip().startswith("#")]


async def open_mcp(stack: AsyncExitStack, urls: list[str]) -> tuple[list[dict], dict]:
    """MCPサーバーに接続し、OpenAI形式のツール定義と「ツール名→セッション」を返す。
    つながらないサーバーは飛ばす"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    tools, route = [], {}
    for url in urls:
        try:
            read, write, _ = await stack.enter_async_context(streamablehttp_client(url, timeout=30))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            for t in (await session.list_tools()).tools:
                if t.name in route:
                    log.warning("MCPツール名が重複しているため後のものを無視: %s (%s)", t.name, url)
                    continue
                route[t.name] = session
                tools.append({"type": "function", "function": {
                    "name": t.name, "description": t.description or "", "parameters": t.inputSchema,
                }})
            log.info("MCP接続: %s（%d個のツール）", url, len(tools))
        except Exception as e:  # noqa: BLE001
            log.warning("MCPに接続できません: %s (%s)", url, e)
    return tools, route


async def call_mcp_tool(route: dict, name: str, args_json: str) -> str:
    if name not in route:
        return f"エラー: {name} というツールはありません"
    try:
        args = json.loads(args_json or "{}")
        res = await route[name].call_tool(name, args)
        text = "\n".join(getattr(c, "text", "") or f"[{c.type}]" for c in res.content)
        if res.isError:
            text = "エラー: " + text
    except Exception as e:  # noqa: BLE001
        text = f"エラー: {e}"
    log.info("MCP %s %s → %d文字", name, args_json, len(text))
    return text[:MCP_RESULT_CHARS]


async def generate_text(cfg: dict, prompt: str) -> str:
    if not cfg["llm_url"]:
        log.warning("LLM URLが未設定のため定型文を使います")
        return cfg["fallback_text"]
    headers = {"Content-Type": "application/json", **llm_headers(cfg)}
    now = dt.datetime.now()
    messages: list[dict] = [{"role": "user", "content": prompt}]
    try:
        async with AsyncExitStack() as stack, httpx.AsyncClient(timeout=300) as c:
            tools, route = await open_mcp(stack, mcp_urls(cfg))
            if tools:
                messages.insert(0, {"role": "system", "content": (
                    f"現在は{now:%Y年%m月%d日} {WEEK[now.weekday()]}曜日 {now:%H時%M分}です。"
                    "あなたは下記のツールを実際に呼び出せます。ニュース・天気・Webページなど最新の情報が必要な"
                    "依頼では、推測や断りを書かずに必ずツールを呼んで取得してください。"
                    "取得が済んだら、電話で読み上げる本文だけを出力してください。"
                )})
            for _ in range(MCP_MAX_ROUNDS + 1):
                body = {
                    **({"model": cfg["llm_model"]} if cfg["llm_model"] else {}),
                    "messages": messages,
                    "temperature": 0.3 if tools and len(messages) <= 2 else 0.8,  # 最初はツールを選ぶので低め
                    "max_tokens": 1500 if tools else 600,
                    "reasoning_effort": "none",   # Gemma 4は思考を切らないと本文が空になる
                }
                if tools:
                    body["tools"] = tools
                r = await c.post(f"{cfg['llm_url']}/v1/chat/completions", json=body, headers=headers)
                r.raise_for_status()
                msg = r.json()["choices"][0]["message"]
                calls = msg.get("tool_calls") or []
                if not calls or not tools:
                    text = msg.get("content") or ""
                    break
                messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
                for tc in calls:
                    fn = tc["function"]
                    out = await call_mcp_tool(route, fn["name"], fn.get("arguments"))
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
            else:
                log.warning("ツール呼び出しが%d回を超えたため打ち切り", MCP_MAX_ROUNDS)
                text = ""
    except Exception as e:  # noqa: BLE001
        log.warning("LLM失敗、定型文を使います: %s", e)
        return cfg["fallback_text"]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"[*#`>\[\]（）()「」]", "", text).strip()
    return text or cfg["fallback_text"]


def tts_headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['tts_token']}"} if cfg["tts_token"] else {}


async def synthesize(cfg: dict, text: str, path: Path) -> float:
    body = {
        "model": "irodori-tts", "input": text,
        "response_format": "wav", "irodori": {"num_steps": int(cfg["tts_steps"])},
    }
    if cfg["voice"]:
        body["voice"] = cfg["voice"]
    if not cfg["tts_url"]:
        raise ValueError("TTS URLが未設定です")
    async with httpx.AsyncClient(timeout=600) as c:
        r = await c.post(f"{cfg['tts_url']}/v1/audio/speech", json=body, headers=tts_headers(cfg))
        r.raise_for_status()
    path.write_bytes(r.content)
    return wav_duration(path)


# baresip 1.1.0のaufileはリサンプルもチャンネル変換もしない。ネゴしたコーデックと
# 違う形式のwavを流すと速さ・音程が狂う（Opusにモノラルを流すと1オクターブ低い声）ので、
# コーデックに合わせて変換してから流す
CODECS = {"opus": ("opus.so", 48000, 2), "g711": ("g711.so", 8000, 1)}


def convert_wav(src: Path, dst: Path, rate: int, channels: int) -> Path:
    """16bit wav を rate Hz / channels ch に変換する"""
    with wave.open(str(src), "rb") as r:
        ch, sw, sr = r.getnchannels(), r.getsampwidth(), r.getframerate()
        frames = r.readframes(r.getnframes())
    if (ch, sw, sr) == (channels, 2, rate):
        return src
    if sw != 2 or ch not in (1, 2):
        raise ValueError(f"非対応のwav: ch={ch} sampwidth={sw}")
    import array
    pcm = array.array("h", frames)
    mono = pcm if ch == 1 else array.array("h", ((a + b) // 2 for a, b in zip(pcm[0::2], pcm[1::2])))
    if sr != rate:
        n = len(mono)
        if rate < sr:  # 縮めるときは折り返しノイズを減らすため、まず移動平均で高域を落とす
            k = max(1, round(sr / rate))
            acc, run = array.array("h", bytes(n * 2)), 0
            for i in range(n):
                run += mono[i] - (mono[i - k] if i >= k else 0)
                acc[i] = run // min(i + 1, k)
            mono = acc
        out_n = int(n * rate / sr)
        step = sr / rate
        res = array.array("h", bytes(out_n * 2))
        for i in range(out_n):  # 線形補間（音声用途なら十分）
            x = i * step
            j = int(x)
            f = x - j
            a = mono[j]
            b = mono[j + 1] if j + 1 < n else a
            res[i] = int(a + (b - a) * f)
        mono = res
    if channels == 2:
        out = array.array("h", bytes(len(mono) * 4))
        out[0::2] = mono
        out[1::2] = mono
    else:
        out = mono
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(out.tobytes())
    return dst


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


async def generate(
    cfg: dict, *, call_at: dt.datetime | None = None, text: str | None = None, prompt: str | None = None,
) -> tuple[str, Path, float]:
    """text があればそのまま読み上げ、無ければ prompt（省略時は設定のプロンプト）でLLMに書かせる"""
    if not text:
        if call_at is None:
            h, m = map(int, cfg["call_at"].split(":"))
            call_at = dt.datetime.combine(dt.date.today(), dt.time(h, m))
        log.info("原稿を生成中…")
        text = await generate_text(cfg, build_prompt(prompt or cfg["prompt"], call_at))
    log.info("原稿: %s", text)
    (OUT / "message.txt").write_text(text)
    path = OUT / "message.wav"
    log.info("音声を合成中…")
    dur = await synthesize(cfg, text, path)
    log.info("音声: %.1f秒", dur)
    return text, path, dur


def llm_headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['llm_token']}"} if cfg["llm_token"] else {}


async def list_models(cfg: dict) -> list[str]:
    if not cfg["llm_url"]:
        return []
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{cfg['llm_url']}/v1/models", headers=llm_headers(cfg))
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []))


async def list_voices(cfg: dict) -> list[str]:
    if not cfg["tts_url"]:
        return []
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{cfg['tts_url']}/v1/audio/voices", headers=tts_headers(cfg))
        r.raise_for_status()
        return [v["id"] for v in r.json().get("data", [])]


def holiday_name(day: dt.date) -> str | None:
    return jpholiday.is_holiday_name(day)


# ---------------------------------------------------------------- SIP（baresip）

BARESIP_MODULES = "/usr/lib/baresip/modules"
CTRL_PORT = 4444
SILENCE = DATA / "silence.wav"


def ensure_silence() -> None:
    """発信直後〜応答までの送話用の無音（10分）"""
    if SILENCE.exists():
        return
    with wave.open(str(SILENCE), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\0\0" * 48000 * 600)


def write_baresip_conf(cfg: dict, confdir: Path, source: Path) -> None:
    (confdir / "config").write_text(f"""\
poll_method		epoll
sip_listen		0.0.0.0:5070
audio_player		aufile,{OUT / 'received.wav'}
audio_source		aufile,{source}
audio_alert		aufile,{OUT / 'alert.wav'}
rtp_ports		46000-46200
module_path		{BARESIP_MODULES}
module			{CODECS[cfg.get("codec", "opus")][0]}
module			aufile.so
module			srtp.so
module			stun.so
module			ice.so
module_app		account.so
module_app		menu.so
module_app		ctrl_tcp.so
ctrl_tcp_listen		127.0.0.1:{CTRL_PORT}
""")
    acc = (
        f"<sip:{cfg['sip_user']}@{cfg['sip_domain']};transport={cfg['sip_transport']}>"
        f";auth_pass={cfg['sip_password']};regint=600"
    )
    if cfg["media_enc"] != "none":
        acc += f";mediaenc={cfg['media_enc']}"
    if cfg["stun_server"] and cfg.get("medianat", "stun") != "none":
        # baresip 1.1.0のiceはstun.linphone.orgで"Protocol error"になるので既定はstun
        acc += f";medianat={cfg.get('medianat', 'stun')};stunserver=stun:{cfg['stun_server']}"
    (confdir / "accounts").write_text(acc + "\n")
    (confdir / "accounts").chmod(0o600)
    (confdir / "contacts").write_text("")


class Ctrl:
    """baresip ctrl_tcp（netstring JSON）"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.r, self.w = reader, writer
        self.events: asyncio.Queue = asyncio.Queue()
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            while True:
                n = int((await self.r.readuntil(b":"))[:-1])
                msg = json.loads((await self.r.readexactly(n + 1))[:-1])
                if msg.get("event"):
                    log.info("SIP: %s %s", msg.get("type"), msg.get("param", ""))
                    await self.events.put(msg)
        except (asyncio.IncompleteReadError, ConnectionError):
            await self.events.put({"type": "CTRL_CLOSED"})

    async def cmd(self, command: str, params: str = "") -> None:
        data = json.dumps({"command": command, "params": params, "token": "x"}).encode()
        self.w.write(str(len(data)).encode() + b":" + data + b",")
        await self.w.drain()

    async def wait(self, types: set[str], timeout: float) -> dict | None:
        end = asyncio.get_running_loop().time() + timeout
        while (left := end - asyncio.get_running_loop().time()) > 0:
            try:
                ev = await asyncio.wait_for(self.events.get(), left)
            except asyncio.TimeoutError:
                return None
            if ev.get("type") in types:
                return ev
        return None

    def close(self) -> None:
        self._task.cancel()
        self.w.close()


async def call(cfg: dict, wav: Path) -> str:
    """発信して、応答したらwavを流し、終わったら切る。戻り値は結果の説明"""
    for k in ("sip_user", "sip_password", "sip_domain", "target"):
        if not cfg[k]:
            return f"{k} が未設定です"
    target = cfg["target"] if cfg["target"].startswith("sip:") else f"sip:{cfg['target']}"
    duration = wav_duration(wav)
    confdir = Path(tempfile.mkdtemp(prefix="baresip-"))
    # 音源は応答した時点で読み込まれる（呼び出し中は流れない）ので最初から原稿を指定する
    _, rate, channels = CODECS[cfg.get("codec", "opus")]
    write_baresip_conf(cfg, confdir, convert_wav(wav, OUT / "message_call.wav", rate, channels))
    proc = await asyncio.create_subprocess_exec(
        "baresip", "-f", str(confdir),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out_task = asyncio.create_task(_pump(proc))
    ctrl = None
    try:
        for _ in range(50):
            try:
                ctrl = Ctrl(*await asyncio.open_connection("127.0.0.1", CTRL_PORT))
                break
            except OSError:
                await asyncio.sleep(0.2)
        if ctrl is None:
            return "baresipに接続できませんでした"

        ev = await ctrl.wait({"REGISTER_OK", "REGISTER_FAIL"}, 20)
        if not ev or ev["type"] != "REGISTER_OK":
            return f"SIP登録に失敗: {ev.get('param') if ev else 'タイムアウト'}"

        log.info("発信 → %s", target)
        await ctrl.cmd("dial", target)
        ev = await ctrl.wait({"CALL_ESTABLISHED", "CALL_CLOSED"}, cfg["ring_timeout"])
        if not ev:
            await ctrl.cmd("hangup")
            return "応答なし"
        if ev["type"] == "CALL_CLOSED":
            return f"接続できず: {ev.get('param', '')}"

        log.info("応答 → 再生開始（%.1f秒）", duration)
        ev = await ctrl.wait({"CALL_CLOSED"}, duration + 1.0)
        if ev:
            if "end of file" in ev.get("param", ""):
                log.info("再生終了、切断")   # baresipは音源の終わりで自分で切る
                return "完了"
            return f"再生中に切断された: {ev.get('param', '')}"
        await ctrl.cmd("hangup")
        await ctrl.wait({"CALL_CLOSED"}, 5)
        log.info("再生終了、切断")
        return "完了"
    finally:
        if ctrl:
            await ctrl.cmd("quit") if not ctrl.w.is_closing() else None
            ctrl.close()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
        out_task.cancel()
        shutil.rmtree(confdir, ignore_errors=True)


async def _pump(proc: asyncio.subprocess.Process) -> None:
    assert proc.stdout
    async for line in proc.stdout:
        s = line.decode(errors="replace").rstrip()
        if s:
            logging.getLogger("baresip").debug(s)
