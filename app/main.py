"""設定WebUI + スケジューラ"""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

import engine
from engine import DEFAULTS, OUT, SECRET_KEYS, load_config, save_config

# ---------------------------------------------------------------- ログ

LOG_BUF: collections.deque[str] = collections.deque(maxlen=500)


class BufHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        LOG_BUF.append(self.format(record))


fmt = logging.Formatter("%(asctime)s %(name)s %(message)s", "%m-%d %H:%M:%S")
root = logging.getLogger()
root.setLevel(logging.INFO)
for h in (BufHandler(), logging.FileHandler(engine.DATA / "morning-call.log"), logging.StreamHandler()):
    h.setFormatter(fmt)
    root.addHandler(h)
logging.getLogger("baresip").setLevel(logging.INFO)  # DEBUGにするとbaresipの出力も残る
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("morning-call")

# ---------------------------------------------------------------- ジョブ

JOB_LOCK = asyncio.Lock()
STATE: dict = {"running": None, "last_result": None, "last_run": None}


def at_today(cfg: dict) -> dt.datetime:
    return dt.datetime.combine(dt.date.today(), dt.time(*map(int, cfg["call_at"].split(":"))))


async def run_job(kind: str, call: dict | None = None) -> str:
    """kind: generate / call / run（手動） / scheduled（定時） / reserved（予約）
    手動は実行中なら409、定時と予約は前の処理が終わるのを待つ"""
    if kind in ("generate", "call", "run") and JOB_LOCK.locked():
        raise HTTPException(409, f"実行中です: {STATE['running']}")
    async with JOB_LOCK:
        STATE["running"] = kind if not call else f"予約 {call['id']}"
        try:
            cfg = load_config()
            if kind in ("scheduled", "reserved"):
                at = at_today(cfg) if kind == "scheduled" else dt.datetime.fromisoformat(call["at"])
                if call:
                    cfg = {**cfg, **{k: call[k] for k in ("target", "voice") if call.get(k)}}
                _, path, _ = await engine.generate(
                    cfg, call_at=at, text=(call or {}).get("verbatim_text") or (call or {}).get("message"),
                    instruction=(call or {}).get("instruction") or (call or {}).get("prompt"))
                wait = (at - dt.datetime.now()).total_seconds()
                if wait > 0:
                    log.info("%sまで%.0f秒待機", at.strftime("%H:%M"), wait)
                    await asyncio.sleep(wait)
                result = await engine.call(cfg, path)
            elif kind == "generate":
                await engine.generate(cfg)
                result = "生成完了"
            elif kind == "call":
                path = OUT / "message.wav"
                if not path.exists():
                    raise HTTPException(400, "先に原稿を生成してください")
                result = await engine.call(cfg, path)
            else:
                _, path, _ = await engine.generate(cfg, call_at=dt.datetime.now())
                result = await engine.call(cfg, path)
            log.info("結果: %s", result)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("失敗")
            result = f"エラー: {e}"
        finally:
            STATE["running"] = None
        STATE["last_result"] = result
        STATE["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
        return result


# ---------------------------------------------------------------- 予約発信（API / MCP 用）

CALLS_PATH = engine.DATA / "calls.json"
CALLS: list[dict] = json.loads(CALLS_PATH.read_text()) if CALLS_PATH.exists() else []
MISSED_AFTER = dt.timedelta(minutes=10)   # 止まっていた等でこれ以上遅れたら発信しない


def save_calls() -> None:
    del CALLS[:-200]  # 古い履歴は捨てる
    tmp = CALLS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(CALLS, ensure_ascii=False, indent=2))
    tmp.replace(CALLS_PATH)


WD_JA = "月火水木金土日"
WD_EN = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
REL_DAYS = {
    "今日": 0, "きょう": 0, "本日": 0, "today": 0,
    "明日": 1, "あした": 1, "あす": 1, "tomorrow": 1,
    "明後日": 2, "あさって": 2, "dayaftertomorrow": 2,
}
WEEK_PREFIX = {  # 週は月曜始まり
    "今週": 0, "thisweek": 0, "来週": 1, "nextweek": 1, "再来週": 2,
}


def parse_relative(at: str, now: dt.datetime) -> dt.datetime | None:
    """「明日 7:30」「明後日7時半」「来週の月曜 8:00」「金曜日 7時」「tomorrow 07:30」「next monday 7:30」。
    時刻を省略すると設定の発信時刻。解釈できなければ None"""
    s = re.sub(r"[\s　]+", " ", at.strip().lower())
    tm = re.search(r"(\d{1,2})(?:[:：](\d{2})|時(?:(\d{1,2})分|(半))?)$", s)
    if tm:
        h = int(tm.group(1))
        mi = 30 if tm.group(4) else int(tm.group(2) or tm.group(3) or 0)
        s = s[: tm.start()]
    else:
        h, mi = map(int, load_config()["call_at"].split(":"))
    s = re.sub(r"\b(?:at|am|on)\b", "", s)
    s = re.sub(r"[\s　の]|午前|朝", "", s)
    if not s:
        return None
    if s in REL_DAYS:
        day = now.date() + dt.timedelta(days=REL_DAYS[s])
    else:
        week = None
        for p, w in WEEK_PREFIX.items():
            if s.startswith(p):
                week, s = w, s[len(p):]
                break
        if s.startswith("next"):
            s = s[4:]
        s = re.sub(r"曜日?$", "", s)
        if len(s) == 1 and s in WD_JA:
            wd = WD_JA.index(s)
        elif s in WD_EN or (len(s) >= 3 and any(e.startswith(s) for e in WD_EN)):
            wd = next(i for i, e in enumerate(WD_EN) if e.startswith(s))
        else:
            return None
        today = now.date()
        if week is None:  # 「金曜」だけなら、今日より後で次に来るその曜日
            day = today + dt.timedelta(days=(wd - today.weekday() - 1) % 7 + 1)
        else:
            monday = today - dt.timedelta(days=today.weekday()) + dt.timedelta(weeks=week)
            day = monday + dt.timedelta(days=wd)
    return dt.datetime.combine(day, dt.time(h, mi))


def parse_at(at: str | None, in_minutes: float | None) -> dt.datetime:
    now = dt.datetime.now()
    if in_minutes is not None:
        return now + dt.timedelta(minutes=float(in_minutes))
    if not at:
        return now
    at = at.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", at):  # 時刻だけなら次に来るその時刻
        t = dt.datetime.combine(now.date(), dt.time(*map(int, at.split(":"))))
        return t if t > now else t + dt.timedelta(days=1)
    if mo := re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[T ](\d{1,2}):(\d{2})(?::\d{2})?", at):
        # 年なし（"11-23 07:30" "11/23 7:30"）は今年。過ぎていれば来年（12月に1月分を予約する場合）
        mon, day, h, mi = map(int, mo.groups())
        t = dt.datetime(now.year, mon, day, h, mi)
        return t if t > now - dt.timedelta(minutes=1) else t.replace(year=now.year + 1)
    if (t := parse_relative(at, now)) is not None:
        return t
    t = dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
    if t.tzinfo:  # タイムゾーン付きはコンテナのローカル時刻に直す
        t = t.astimezone().replace(tzinfo=None)
    return t


def create_call(
    at: str | None = None, in_minutes: float | None = None, message: str | None = None,
    prompt: str | None = None, target: str | None = None, voice: str | None = None,
) -> dict:
    try:
        when = parse_at(at, in_minutes)
    except ValueError:
        raise HTTPException(400, f"時刻を解釈できません: {at}（\"MM-DD HH:MM\"、\"HH:MM\"、ISO 8601）")
    now = dt.datetime.now()
    if when < now - dt.timedelta(minutes=1):
        raise HTTPException(
            400, f"過去の時刻です: {when.isoformat(timespec='minutes')}（現在 {now.isoformat(timespec='minutes')}）")
    if message and prompt:
        raise HTTPException(400, "message と prompt はどちらか一方だけ指定してください")
    call = {
        "id": uuid.uuid4().hex[:8], "at": when.isoformat(timespec="seconds"),
        "at_text": f"{when:%Y-%m-%d}({WD_JA[when.weekday()]}) {when:%H:%M}",
        "instruction": prompt or None, "verbatim_text": message or None,
        "target": target or None, "voice": voice or None,
        "status": "pending", "result": None, "created": dt.datetime.now().isoformat(timespec="seconds"),
    }
    CALLS.append(call)
    save_calls()
    log.info("予約を受け付けました: %s %s", call["id"], call["at"])
    return call


def _cancel(call_id: str) -> dict:
    for c in CALLS:
        if c["id"] == call_id:
            if c["status"] != "pending":
                raise HTTPException(409, f"取り消せません（{c['status']}）")
            c["status"] = "cancelled"
            save_calls()
            log.info("予約を取り消しました: %s", call_id)
            return c
    raise HTTPException(404, f"予約が見つかりません: {call_id}")


async def run_reserved(call: dict) -> None:
    result = await run_job("reserved", call)
    call["status"] = "done" if result in ("完了",) or result.startswith("完了") else "failed"
    call["result"] = result
    save_calls()


def next_run(cfg: dict, now: dt.datetime | None = None) -> tuple[dt.datetime, str | None] | None:
    """次に発信する日時（定時と予約の早いほう）"""
    now = now or dt.datetime.now()
    cands = [dt.datetime.fromisoformat(c["at"]) for c in CALLS if c["status"] == "pending"]
    if cfg["enabled"] and cfg["weekdays"]:
        h, m = map(int, cfg["call_at"].split(":"))
        for i in range(0, 15):
            day = now.date() + dt.timedelta(days=i)
            at = dt.datetime.combine(day, dt.time(h, m))
            if at <= now or day.weekday() not in cfg["weekdays"]:
                continue
            if cfg["skip_holidays"] and engine.holiday_name(day):
                continue
            cands.append(at)
            break
    return (min(cands), None) if cands else None


async def scheduler() -> None:
    fired: set[str] = set()
    while True:
        try:
            cfg = load_config()
            now = dt.datetime.now()
            prep = dt.timedelta(minutes=int(cfg["prepare_minutes"]))
            at = at_today(cfg)
            key = now.date().isoformat()
            if (
                cfg["enabled"] and at - prep <= now < at and key not in fired
                and now.weekday() in cfg["weekdays"]
            ):
                fired.add(key)
                if cfg["skip_holidays"] and (name := engine.holiday_name(now.date())):
                    log.info("祝日のためスキップ: %s", name)
                else:
                    asyncio.create_task(run_job("scheduled"))
            for c in CALLS:
                if c["status"] != "pending":
                    continue
                c_at = dt.datetime.fromisoformat(c["at"])
                if now > c_at + MISSED_AFTER:
                    c["status"], c["result"] = "missed", "予定時刻を過ぎていたため発信しませんでした"
                    save_calls()
                    log.warning("予約 %s は時刻を過ぎていたためスキップ", c["id"])
                elif now >= c_at - prep:
                    c["status"] = "running"
                    save_calls()
                    asyncio.create_task(run_reserved(c))
        except Exception:  # noqa: BLE001
            log.exception("スケジューラでエラー")
        await asyncio.sleep(5)


# ---------------------------------------------------------------- MCP

# LAN内の別マシン（OpenClaw等）から呼べるよう Host ヘッダの制限は外す。インターネットには公開しないこと
mcp = FastMCP(
    "morning-call", stateless_http=True, json_response=True, streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def schedule_call(
    instruction: str | None = None, at: str | None = None, in_minutes: float | None = None,
    verbatim_text: str | None = None, target: str | None = None, voice: str | None = None,
) -> dict:
    """Reserve a phone call to the user. At call time the user's phone rings; when they answer,
    a voice reads a message, then the call hangs up.

    The message is written by this call system's own LLM a few minutes BEFORE the call, and that
    LLM can fetch news, weather, web pages etc. by itself at that moment. So:
    - Put what the user wants the call to say/do in `instruction`, in the user's own words
      (e.g. "最新ニュースを3つ伝えて", "会議があることを伝えて起こして").
    - Do NOT fetch news/weather yourself and do NOT write the message yourself — it would be stale
      by call time. Leave the writing to this system.

    Args:
        instruction: The user's request for what the call should say. Recommended for almost all
            calls. If both `instruction` and `verbatim_text` are omitted, the configured default
            morning-call message is used.
        at: When to call (server local time). Prefer formats WITHOUT a year: "HH:MM" (next
            occurrence), "MM-DD HH:MM" (this year; next year if already past), "tomorrow 07:30",
            "day after tomorrow 07:30", "friday 07:30" (next Friday after today),
            "this week friday 07:30", "next week monday 07:30" (weeks start on Monday).
            Japanese works too: "明日 7:30", "明後日 7時半", "来週の月曜 8:00", "金曜日 7時".
            Time omitted ("明日") = the configured daily call time. ISO 8601 also works, but only
            include a year if you are sure of the current date. Omit both `at` and `in_minutes`
            to call right away.
        in_minutes: Call this many minutes from now (alternative to `at`).
        verbatim_text: ONLY when the user dictated the exact words to be read aloud. Read as-is,
            no LLM, no tools. Never use it for news, weather or anything time-sensitive.
        target: SIP address to call (defaults to the configured target).
        voice: TTS voice id (defaults to the configured voice).

    Returns the reservation with its `id` (for cancel_call) and `at_text` (resolved date/time with
    weekday) — check `at_text` against what the user asked for.
    """
    return create_call(at, in_minutes, verbatim_text, instruction, target, voice)


@mcp.tool()
def list_calls(include_finished: bool = False) -> list[dict]:
    """List reserved calls. Pending ones only, unless include_finished is true."""
    return [c for c in CALLS if include_finished or c["status"] in ("pending", "running")]


@mcp.tool()
def cancel_call(call_id: str) -> dict:
    """Cancel a pending reserved call by id."""
    return _cancel(call_id)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for c in CALLS:  # 処理中に落ちていた予約は失敗扱い
        if c["status"] == "running":
            c["status"], c["result"] = "failed", "処理中にサーバーが再起動しました"
    task = asyncio.create_task(scheduler())
    log.info("起動しました")
    async with mcp.session_manager.run():
        yield
    task.cancel()


app = FastAPI(title="morning-call", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"

# ---------------------------------------------------------------- API


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (STATIC / "index.html").read_text()


@app.get("/api/config")
async def get_config() -> dict:
    cfg = load_config()
    for k in SECRET_KEYS:
        cfg[k] = "********" if cfg[k] else ""
    return cfg


@app.put("/api/config")
async def put_config(body: dict) -> dict:
    cfg = load_config()
    for k, v in body.items():
        if k not in DEFAULTS:
            continue
        if k in SECRET_KEYS and v == "********":
            continue  # 伏せ字のまま送られてきたら変更しない
        cfg[k] = v
    try:
        h, m = map(int, cfg["call_at"].split(":"))
        assert 0 <= h < 24 and 0 <= m < 60
    except Exception:
        raise HTTPException(400, "時刻は HH:MM で指定してください")
    save_config(cfg)
    log.info("設定を保存しました")
    return {"ok": True}


@app.post("/api/config/reset-prompt")
async def reset_prompt() -> dict:
    cfg = load_config()
    cfg["prompt"] = engine.DEFAULT_PROMPT
    save_config(cfg)
    return {"prompt": cfg["prompt"]}


@app.get("/api/status")
async def status() -> dict:
    cfg = load_config()
    nr = next_run(cfg)
    msg = OUT / "message.txt"
    return {
        **STATE,
        "now": dt.datetime.now().isoformat(timespec="seconds"),
        "next_run": nr[0].isoformat(timespec="minutes") if nr else None,
        "message_text": msg.read_text() if msg.exists() else None,
        "message_mtime": dt.datetime.fromtimestamp(msg.stat().st_mtime).isoformat(timespec="seconds") if msg.exists() else None,
    }


@app.get("/api/log")
async def get_log() -> dict:
    return {"lines": list(LOG_BUF)[-200:]}


def cfg_with(body: dict) -> dict:
    """保存前のフォームの値（URL・トークン）で一覧を取れるように上書きする"""
    cfg = load_config()
    for k in ("llm_url", "llm_token", "tts_url", "tts_token"):
        v = body.get(k)
        if v is not None and not (k in SECRET_KEYS and v == "********"):
            cfg[k] = v.rstrip("/") if k.endswith("_url") else v
    return cfg


@app.post("/api/voices")
async def voices(body: dict) -> dict:
    try:
        return {"voices": await engine.list_voices(cfg_with(body))}
    except Exception as e:  # noqa: BLE001
        return {"voices": [], "error": str(e)}


@app.post("/api/models")
async def models(body: dict) -> dict:
    try:
        return {"models": await engine.list_models(cfg_with(body))}
    except Exception as e:  # noqa: BLE001
        return {"models": [], "error": str(e)}


@app.get("/api/holidays")
async def holidays() -> dict:
    today = dt.date.today()
    days = [today + dt.timedelta(days=i) for i in range(60)]
    return {"holidays": [{"date": d.isoformat(), "name": n} for d in days if (n := engine.holiday_name(d))]}


@app.get("/audio/message.wav")
async def audio() -> FileResponse:
    p = OUT / "message.wav"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="audio/wav", headers={"Cache-Control": "no-store"})


class CallRequest(BaseModel):
    at: str | None = Field(None, description='"HH:MM"（次に来るその時刻）、"MM-DD HH:MM"（今年。過ぎていれば来年）、"明日 7:30"・"明後日 7時半"・"来週の月曜 8:00"・"金曜 7時"・"tomorrow 07:30"・"next week monday 07:30"、ISO 8601。時刻省略で設定の発信時刻。at も in_minutes も無ければ今すぐ')
    in_minutes: float | None = Field(None, description="何分後にかけるか（at の代わり）")
    instruction: str | None = Field(None, description="何を伝えてほしいか（例「最新ニュースを3つ伝えて」）。発信の数分前にLLMが原稿を書き、必要ならMCPツールでニュース等を取得する")
    verbatim_text: str | None = Field(None, description="一字一句そのまま読み上げる文（LLMもツールも使わない）")
    prompt: str | None = Field(None, description="instruction の旧名", deprecated=True)
    message: str | None = Field(None, description="verbatim_text の旧名", deprecated=True)
    target: str | None = Field(None, description="発信先SIPアドレス（省略で設定値）")
    voice: str | None = Field(None, description="TTSの声（省略で設定値）")


@app.post("/api/calls", status_code=201)
async def api_create_call(req: CallRequest) -> dict:
    """指定時刻に電話をかけて読み上げる予約を作る"""
    return create_call(
        req.at, req.in_minutes, req.verbatim_text or req.message, req.instruction or req.prompt, req.target, req.voice)


@app.get("/api/calls")
async def api_list_calls(include_finished: bool = True) -> list[dict]:
    return list_calls(include_finished)


@app.delete("/api/calls/{call_id}")
async def api_cancel_call(call_id: str) -> dict:
    return _cancel(call_id)


@app.post("/api/run/{kind}")
async def run(kind: str) -> dict:
    if kind not in ("generate", "call", "run"):
        raise HTTPException(404)
    if JOB_LOCK.locked():
        raise HTTPException(409, f"実行中です: {STATE['running']}")
    asyncio.create_task(run_job(kind))
    return {"started": kind}


# MCP（Streamable HTTP: POST /mcp）。他のルートより後に登録する
app.mount("/", mcp.streamable_http_app())
