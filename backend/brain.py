"""The "brain": conversation with Claude (tool use) + sync across every device.

Latency design (time from "user stops talking" to "J.A.R.V.I.S. starts talking"):
- the reply is STREAMED: each sentence is sent to the speaking device as soon as it is complete,
  so text-to-speech starts on the first sentence while Claude is still writing the rest;
- the device's local time and current weather are put straight into the context, so the most
  common questions need no tool round-trip (each one is a whole extra model call).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import anthropic

from .config import Settings
from .hub import Client, Hub
from .i18n import lang
from .memory import Memory
from .skills import SkillContext, SkillRegistry
from .skills.weather import fetch_forecast, fetch_weather

log = logging.getLogger("jarvis.brain")

MAX_TOOL_ROUNDS = 25  # coding tasks chain many file/terminal steps
MAX_JSON_RETRIES = 2
CONTEXT_WEATHER_TIMEOUT = 2.0  # seconds; the weather is usually cached, never block a reply on it
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Commands that should simply happen, without J.A.R.V.I.S. talking about them ("open Amazon", "pause", "louder")
QUICK_ACTIONS = {"system_control", "play_music", "media_control", "app_volume", "remember_fact", "forget_fact"}
# Up to this many characters of text are held back until we know whether a quick action follows
HOLD_CHARS = 140
# Models that support server-side refusal fallbacks (Haiku does not)
FALLBACK_MODEL_PREFIXES = ("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5", "claude-mythos-5")

# The persona and every user-facing message live in i18n.py (English / Italian modes).


def supports_fallback(model: str) -> bool:
    return model.startswith(FALLBACK_MODEL_PREFIXES)


class SentenceSplitter:
    """Cuts streamed text into speakable sentences."""

    END = re.compile(r"(.+?[.!?…:;])(\s+|$)", re.S)
    MIN_CHARS = 18  # avoid tiny fragments like "Mr." or "Yes." becoming separate audio clips

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out: list[str] = []
        while True:
            m = self.END.match(self.buf)
            if not m or not m.group(2):  # need trailing whitespace to be sure the sentence ended
                break
            if len(m.group(1).strip()) < self.MIN_CHARS and len(self.buf) < 200:
                # too short alone: wait to join it with the next sentence
                nxt = self.END.match(self.buf, m.end())
                if not nxt or not nxt.group(2):
                    break
                out.append(self.buf[:nxt.end(1)].strip())
                self.buf = self.buf[nxt.end():]
                continue
            out.append(m.group(1).strip())
            self.buf = self.buf[m.end():]
        return out

    def flush(self) -> str:
        rest, self.buf = self.buf.strip(), ""
        return rest


class Brain:
    def __init__(self, settings: Settings, memory: Memory, registry: SkillRegistry, hub: Hub):
        self.settings = settings
        self.memory = memory
        self.registry = registry
        self.hub = hub
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._client: anthropic.AsyncAnthropic | None = None
        self._web_search_ok = True

    # ---------- Anthropic client (created lazily: the server starts even without a key) ----------
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            kwargs = {"api_key": self.settings.anthropic_api_key} if self.settings.anthropic_api_key else {}
            self._client = anthropic.AsyncAnthropic(max_retries=2, timeout=120, **kwargs)
        return self._client

    # ---------- prompt ----------
    async def _live_context(self, origin: Client | None) -> list[str]:
        """Local time and current weather of the speaking device, so no tool call is needed for them."""
        tz_name = (origin.timezone if origin else None) or self.settings.timezone
        try:
            now = datetime.now(ZoneInfo(tz_name))
        except Exception:
            now, tz_name = datetime.now(), "server time"
        lines = [f"Current local date and time on the user's device: {now:%A %d %B %Y, %H:%M} ({tz_name})."]
        try:
            if origin and origin.location:
                wx = await asyncio.wait_for(fetch_forecast(origin.location, 3, lang.code), CONTEXT_WEATHER_TIMEOUT)
            else:
                wx = await asyncio.wait_for(fetch_weather(self.settings.default_city, 3, lang.code), CONTEXT_WEATHER_TIMEOUT)
            c, days = wx["current"], wx["forecast"]
            lines.append(
                f"Current weather at {wx['location']}: {c['temperature_c']:.0f}°C, {c['conditions']}, feels like "
                f"{c['feels_like_c']:.0f}°C, humidity {c['humidity_pct']}%, wind {c['wind_kmh']:.0f} km/h. "
                + " ".join(f"{d['date']}: {d['conditions']}, {d['min_c']:.0f}-{d['max_c']:.0f}°C, "
                           f"rain {d['rain_chance_pct'] or 0}%." for d in days))
        except Exception as exc:
            log.debug("Live weather context unavailable: %s", exc)
        lines.append("Answer questions about the current time or the weather here directly from these values; "
                     "use the tools only for other places, other dates or anything not listed.")
        return lines

    async def _system(self, origin: Client | None = None) -> list[dict]:
        blocks = [{
            "type": "text",
            "text": lang.profile.persona.format(title=lang.profile.title),
            "cache_control": {"type": "ephemeral"},
        }]
        facts, live = await asyncio.gather(self.memory.facts(), self._live_context(origin))
        if origin and origin.location:
            extra = [f"The user is currently speaking from {origin.device_name}, physically located in "
                     f"{origin.location['label']} (real device position). Use this place for anything local "
                     f"unless they name another one."]
        else:
            extra = [f"The user's position is unknown; default city: {self.settings.default_city}."]
        extra += live
        if facts:
            extra.append("Long-term memory (known facts about the user):\n" +
                         "\n".join(f"- {k}: {v}" for k, v in facts.items()))
        blocks.append({"type": "text", "text": "\n\n".join(extra)})
        return blocks

    async def _history_messages(self, session_id: str) -> list[dict]:
        rows = await self.memory.history(session_id, self.settings.history_limit)
        msgs: list[dict] = []
        for r in rows:
            text = r["content"]
            if r["role"] == "user":
                text = f"[device: {r['device_name'] or 'unknown'}] {text}"
            if msgs and msgs[-1]["role"] == r["role"]:  # merge consecutive turns of the same role
                msgs[-1]["content"] += "\n" + text
            else:
                msgs.append({"role": r["role"], "content": text})
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    # ---------- HUD log ----------
    async def _skill_log(self, session_id: str, label: str, status: str, detail: str = "", origin: Client | None = None):
        await self.hub.broadcast({
            "type": "skill_log", "skill": label, "status": status, "detail": detail[:160],
            "device": origin.device_name if origin else "", "ts": time.time(),
        }, session_id)

    # ---------- tool execution ----------
    async def _run_tool(self, block, ctx: SkillContext) -> dict:
        skill = self.registry.skills.get(block.name)
        label = skill.hud_label if skill else block.name.upper()
        # Streamed tool inputs are not validated by the API: check them before running anything
        problem = self.registry.validate(block.name, block.input)
        if problem:
            await self._skill_log(ctx.session_id, label, "FAIL", problem, ctx.origin)
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": json.dumps({"INVALID_INPUT": problem, "received": block.input}, default=str)}
        await self._skill_log(ctx.session_id, label, "RUN", json.dumps(block.input, ensure_ascii=False), ctx.origin)
        try:
            result = await self.registry.execute(block.name, block.input, ctx)
            failed = isinstance(result, dict) and "error" in result
            await self._skill_log(ctx.session_id, label, "FAIL" if failed else "OK", "", ctx.origin)
            image, media_type = None, "image/jpeg"
            if isinstance(result, dict):
                image = result.pop("_image_b64", None) or result.pop("_image_jpeg_b64", None)
                media_type = result.pop("_image_media_type", media_type)
            text = json.dumps(result, ensure_ascii=False, default=str)
            content = text if not image else [  # screenshot: the model really sees it
                {"type": "text", "text": text},
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image}},
            ]
            return {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": failed}
        except Exception as exc:  # the error goes back to the model, which explains it to the user
            log.exception("Skill %s failed", block.name)
            await self._skill_log(ctx.session_id, label, "FAIL", str(exc), ctx.origin)
            return {"type": "tool_result", "tool_use_id": block.id, "content": f"Error: {exc}", "is_error": True}

    def _tools(self) -> list[dict]:
        tools = []
        for t in self.registry.tool_definitions(include_server_tools=self._web_search_ok):
            # custom tools have no "type": stream their inputs as they are generated
            tools.append(t if "type" in t else {**t, "eager_input_streaming": True})
        return tools

    async def _stream_claude(self, system, messages, on_text: Callable[[str], Awaitable[None]]):
        """One streamed model call; calls on_text for every text fragment. Returns the final message."""
        kwargs = dict(
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            system=system,
            tools=self._tools(),
            messages=messages,
            output_config={"effort": self.settings.effort},
        )
        if supports_fallback(self.settings.model):
            # On a refusal the API re-runs the request on a fallback model inside the same call
            stream_fn = self.client().beta.messages.stream
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        else:
            stream_fn = self.client().messages.stream

        json_retries = 0
        while True:
            try:
                async with stream_fn(**kwargs) as stream:
                    async for event in stream:
                        if event.type == "text":
                            await on_text(event.text)
                    return await stream.get_final_message()
            except ValueError:
                # tool input JSON the SDK could not parse at all: re-issue the turn (bounded)
                json_retries += 1
                if json_retries > MAX_JSON_RETRIES:
                    raise
            except anthropic.BadRequestError as exc:
                # Web search not enabled for the organisation: retry without server tools
                if self._web_search_ok and "web_search" in str(exc):
                    log.warning("Web search unavailable, disabling it: %s", exc)
                    self._web_search_ok = False
                    kwargs["tools"] = self._tools()
                    continue
                raise

    # ---------- conversation turn ----------
    async def handle_user_message(self, session_id: str, text: str, origin: Client) -> None:
        text = text.strip()
        if not text:
            return
        async with self._locks[session_id]:
            t0 = time.perf_counter()
            turn = uuid.uuid4().hex[:10]
            user_row = await self.memory.add_message(session_id, "user", text, origin.device_id, origin.device_name)
            await self.hub.broadcast({"type": "message", "message": user_row}, session_id)
            await self.hub.broadcast({"type": "state", "state": "thinking", "device": origin.device_name}, session_id)

            spoken: list[str] = []
            unspoken: list[str] = []   # short confirmations after silent actions: shown in the chat, not said
            first_audio: list[float] = []

            async def speak(sentence: str) -> None:
                """Send one sentence to the device that asked: it starts speaking right away."""
                if not sentence:
                    return
                if not first_audio:
                    first_audio.append(time.perf_counter() - t0)
                spoken.append(sentence)
                await self.hub.send(origin.client_id, {"type": "speak_chunk", "turn": turn, "text": sentence})
                await self.hub.broadcast({"type": "partial", "turn": turn, "text": " ".join(spoken)}, session_id)

            async def note(sentence: str) -> None:
                if sentence:
                    unspoken.append(sentence)

            try:
                problem, acted = await self._agent_loop(session_id, origin, speak, note)
                if problem:              # errors / refusals are always said, even after a partial answer
                    await speak(problem)
                elif not spoken and not acted and not unspoken:   # nothing done and nothing said
                    await speak(lang.profile.msg("done"))
            except asyncio.CancelledError:
                # The user talked over J.A.R.V.I.S.: keep what was already said, so the history stays coherent
                log.info("Turn interrupted after %.2fs", time.perf_counter() - t0)
                await self.hub.send(origin.client_id, {"type": "speak_end", "turn": turn})
                if spoken:
                    row = await self.memory.add_message(session_id, "assistant", " ".join(spoken) + " …", None, "J.A.R.V.I.S.")
                    await self.hub.broadcast({"type": "message", "message": row, "turn": turn}, session_id)
                await self.hub.broadcast({"type": "state", "state": "idle"}, session_id)
                raise
            silent = not spoken
            # silent=True: the action was done without a word; the HUD plays a short "done" sound instead
            await self.hub.send(origin.client_id, {"type": "speak_end", "turn": turn, "silent": silent})

            final_text = " ".join(spoken) or " ".join(unspoken) or "✓"
            row = await self.memory.add_message(session_id, "assistant", final_text, None, "J.A.R.V.I.S.")
            await self.hub.broadcast({"type": "message", "message": row, "turn": turn}, session_id)
            await self.hub.broadcast({"type": "state", "state": "idle"}, session_id)
            log.info("Turn done%s: first sentence after %.2fs, total %.2fs", " (silent action)" if silent else "",
                     first_audio[0] if first_audio else -1, time.perf_counter() - t0)

    async def _agent_loop(self, session_id: str, origin: Client, speak: Callable[[str], Awaitable[None]],
                          note: Callable[[str], Awaitable[None]]) -> tuple[str | None, bool]:
        """Runs the tool loop, streaming sentences through `speak`.

        Returns (error message or None, whether any tool was used).
        Silent actions: short text written just before a quick action ("Opening Amazon.") is held back and
        dropped, and a short closing line after only quick actions is kept out of the voice (`note`), so
        "open Amazon" simply happens. Longer text is always spoken as it streams.
        """
        p = lang.profile
        ctx = SkillContext(session_id=session_id, origin=origin, hub=self.hub, memory=self.memory, settings=self.settings)
        splitter = SentenceSplitter()
        held: list[str] = []
        holding = [True]          # per model call: keep short text until we know whether an action follows
        only_quick_actions = [True]
        acted = [False]

        async def release() -> None:
            for s in held:
                await speak(s)
            held.clear()

        async def on_text(fragment: str) -> None:
            for sentence in splitter.feed(fragment):
                if holding[0] and sum(map(len, held)) + len(sentence) <= HOLD_CHARS:
                    held.append(sentence)
                    continue
                holding[0] = False       # a real answer is coming: speak it live
                await release()
                await speak(sentence)

        try:
            system, messages = await asyncio.gather(self._system(origin), self._history_messages(session_id))
            for _ in range(MAX_TOOL_ROUNDS):
                held.clear()
                holding[0] = True
                resp = await self._stream_claude(system, messages, on_text)
                rest = splitter.flush()
                if rest:
                    if holding[0]:
                        held.append(rest)
                    else:
                        await speak(rest)

                for b in resp.content:  # log server tools (web search)
                    if b.type == "server_tool_use":
                        q = (b.input or {}).get("query", "") if isinstance(b.input, dict) else ""
                        await self._skill_log(session_id, b.name.upper(), "OK", q, origin)

                if resp.stop_reason == "refusal":
                    held.clear()
                    return p.msg("refusal"), acted[0]
                if resp.stop_reason == "pause_turn":
                    await release()
                    messages.append({"role": "assistant", "content": resp.content})
                    continue
                tool_blocks = [b for b in resp.content if b.type == "tool_use"]
                if resp.stop_reason == "max_tokens" and tool_blocks:
                    return p.msg("internal"), acted[0]  # a truncated tool input must never be run
                if resp.stop_reason == "tool_use" and tool_blocks:
                    quick = all(b.name in QUICK_ACTIONS for b in tool_blocks)
                    if quick:
                        held.clear()             # "Opening Amazon." before a quick action: not said
                    else:
                        await release()          # narration before a longer task: said
                    messages.append({"role": "assistant", "content": resp.content})
                    results = await asyncio.gather(*(self._run_tool(b, ctx) for b in tool_blocks))
                    acted[0] = True
                    if not quick or any(r.get("is_error") for r in results):
                        only_quick_actions[0] = False
                    messages.append({"role": "user", "content": list(results)})
                    continue
                # final answer of the turn
                if acted[0] and only_quick_actions[0] and held and sum(map(len, held)) <= HOLD_CHARS:
                    for s in held:               # "Done, sir." after a quick action: shown, not said
                        await note(s)
                    held.clear()
                else:
                    await release()
                return None, acted[0]
            await release()
            return p.msg("too_many_steps"), acted[0]
        except anthropic.AuthenticationError:
            return p.msg("auth"), acted[0]
        except anthropic.RateLimitError:
            return p.msg("rate"), acted[0]
        except anthropic.APIConnectionError:
            return p.msg("connection"), acted[0]
        except anthropic.APIStatusError as exc:
            log.error("API error %s: %s", exc.status_code, exc)
            return p.msg("status", code=exc.status_code), acted[0]
        except anthropic.AnthropicError as exc:
            log.error("Anthropic client error: %s", exc)
            return p.msg("client"), acted[0]
        except TypeError as exc:  # the SDK raises TypeError when it finds no credentials at all
            if "authentication" not in str(exc):
                log.exception("Unexpected error")
                return p.msg("internal"), acted[0]
            log.error("No Anthropic credentials: %s", exc)
            return p.msg("no_credentials"), acted[0]
        except Exception:
            log.exception("Unexpected error in the conversation loop")
            return p.msg("internal"), acted[0]
