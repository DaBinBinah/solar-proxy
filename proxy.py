#!/usr/bin/env python3
"""
solar-proxy — 把 Upstage solar 系列模型代理成 Anthropic Messages API，供 Claude Code 使用。

  Claude Code (Anthropic 协议) → 本代理 (127.0.0.1:8787) → Upstage (OpenAI 协议)

支持：流式/非流式、function calling（tool_use/tool_result 双向转换）、模型映射
（claude-*haiku* → solar-mini4，其余 → solar-pro4）、/v1/messages/count_tokens 估算。

仅依赖：Python 3.9+ 标准库 + requests。
环境变量：UPSTAGE_API_KEY、PORT(默认8787)、BIG_MODEL(默认solar-pro4)、SMALL_MODEL(默认solar-mini4)
"""
import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

UPSTAGE_API_KEY = os.environ.get("UPSTAGE_API_KEY", "")
UPSTAGE_BASE = os.environ.get("UPSTAGE_BASE_URL", "https://api.upstage.ai/v1")
PORT = int(os.environ.get("PORT", "8787"))
BIG_MODEL = os.environ.get("BIG_MODEL", "solar-pro4")
SMALL_MODEL = os.environ.get("SMALL_MODEL", "solar-mini4")
VERBOSE = os.environ.get("VERBOSE", "1") == "1"


def log(*a):
    if VERBOSE:
        print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def map_model(an_thropic_model: str) -> str:
    if not an_thropic_model:
        return BIG_MODEL
    m = an_thropic_model.lower()
    if m.startswith("solar"):
        return an_thropic_model  # 已是 solar 模型名，直接透传
    if "haiku" in m:
        return SMALL_MODEL
    return BIG_MODEL


# ─────────────────────────── 请求转换：Anthropic → OpenAI ───────────────────────────

def _text_of_content(content) -> str:
    """Anthropic content（字符串或块数组）→ 纯文本（取 text 部分）"""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "\n".join(parts)


def convert_request(payload: dict):
    """返回 (openai_payload, est_input_chars)"""
    msgs_out, char_count = [], 0
    id_map = {}  # toolu_* → call_*（部分模型对非标准 id 的历史工具消息会模板错乱）

    def rid(tid):
        if not tid:
            return tid
        if tid.startswith("call_"):
            return tid  # 已是标准格式，透传
        if tid not in id_map:
            id_map[tid] = "call_" + uuid.uuid4().hex[:12]
        return id_map[tid]

    # system
    system_text = _text_of_content(payload.get("system"))
    if system_text:
        msgs_out.append({"role": "system", "content": system_text})
        char_count += len(system_text)

    for msg in payload.get("messages", []):
        role, content = msg.get("role"), msg.get("content")
        char_count += len(_text_of_content(content))
        if isinstance(content, str):
            msgs_out.append({"role": role, "content": content})
            continue

        if role == "assistant":
            # text 块合并为 content；tool_use 块转为 tool_calls
            text_parts, tool_calls = [], []
            for b in content:
                t = b.get("type")
                if t == "text":
                    text_parts.append(b.get("text", ""))
                elif t == "tool_use":
                    tool_calls.append({
                        "id": rid(b.get("id") or None),
                        "type": "function",
                        "function": {"name": b.get("name", ""),
                                     "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)},
                    })
                # thinking / redacted_thinking 块丢弃
            m = {"role": "assistant", "content": "\n".join(text_parts)}
            if tool_calls:
                m["tool_calls"] = tool_calls
            msgs_out.append(m)

        else:  # user
            # tool_result → 独立 role:"tool" 消息（必须紧跟对应 assistant）；其余 text 归 user
            pending_text = []
            for b in content:
                t = b.get("type")
                if t == "tool_result":
                    inner = b.get("content")
                    if isinstance(inner, str):
                        tool_text = inner
                    else:
                        tool_text = "\n".join(
                            x.get("text", "") for x in (inner or []) if isinstance(x, dict))
                    prefix = "[工具执行出错] " if b.get("is_error") else ""
                    msgs_out.append({"role": "tool",
                                     "tool_call_id": rid(b.get("tool_use_id", "")),
                                     "content": prefix + tool_text})
                    char_count += len(tool_text)
                elif t == "text":
                    pending_text.append(b.get("text", ""))
                # image / thinking 等块丢弃（solar 为纯文本模型）
            if pending_text:
                msgs_out.append({"role": "user", "content": "\n".join(pending_text)})

    out = {"model": map_model(payload.get("model", "")),
           "messages": msgs_out,
           "max_tokens": min(int(payload.get("max_tokens") or 4096), 32768)}

    if payload.get("temperature") is not None:
        out["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        out["top_p"] = payload["top_p"]
    if payload.get("stop_sequences"):
        out["stop"] = payload["stop_sequences"]

    # thinking 参数 → reasoning_effort（按预算粗略映射）
    thinking = payload.get("thinking") or {}
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        budget = thinking.get("budget_tokens", 8192)
        out["reasoning_effort"] = "low" if budget < 4096 else ("medium" if budget < 20000 else "high")

    # tools
    tools = []
    for t in payload.get("tools", []):
        params = dict(t.get("input_schema") or {})
        params.pop("$schema", None)
        tools.append({"type": "function",
                      "function": {"name": t.get("name", ""),
                                   "description": t.get("description", ""),
                                   "parameters": params}})
    if tools:
        out["tools"] = tools
        char_count += sum(len(json.dumps(t, ensure_ascii=False)) for t in payload.get("tools", [])) // 2

    tc = payload.get("tool_choice")
    if tc:
        ty = tc.get("type")
        if ty == "auto":
            out["tool_choice"] = "auto"
        elif ty == "any":
            out["tool_choice"] = "required"
        elif ty == "tool":
            out["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}

    return out, char_count


# ─────────────────────────── 响应转换：OpenAI → Anthropic ───────────────────────────

def stop_reason_of(finish: str) -> str:
    return {"tool_calls": "tool_use", "length": "max_tokens"}.get(finish or "", "end_turn")


def convert_response_nonstream(openai_resp: dict, requested_model: str) -> dict:
    choice = (openai_resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments", "")}
        content.append({"type": "tool_use", "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                        "name": fn.get("name", ""), "input": args})
    if not content:
        content = [{"type": "text", "text": "(空响应)"}]
    usage = openai_resp.get("usage") or {}
    return {
        "id": "msg_" + uuid.uuid4().hex[:24],
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content,
        "stop_reason": stop_reason_of(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0),
                  "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0},
    }


class StreamTransformer:
    """把 OpenAI SSE 流改写为 Anthropic SSE 流"""

    def __init__(self, requested_model: str, est_input_tokens: int):
        self.model = requested_model
        self.est_input = est_input_tokens
        self.next_index = 0
        self.text_open = None          # 当前 text 块 index 或 None
        self.tool_blocks = {}          # openai tool index → {"block": idx, "opened": bool}
        self.out_chars = 0
        self.final_usage = None

    def sse(self, event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    def start(self) -> bytes:
        msg = {"type": "message", "id": "msg_" + uuid.uuid4().hex[:24], "role": "assistant",
               "model": self.model, "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": self.est_input, "output_tokens": 0,
                         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        return (self.sse("message_start", {"type": "message_start", "message": msg})
                + self.sse("ping", {"type": "ping"}))

    def _open_text(self):
        self.text_open = self.next_index
        self.next_index += 1
        return self.sse("content_block_start", {"type": "content_block_start", "index": self.text_open,
                                                "content_block": {"type": "text", "text": ""}})

    def on_chunk(self, chunk: dict):
        """处理一个 OpenAI chunk，返回要下发的字节（可能为空）"""
        out = b""
        if chunk.get("usage"):
            self.final_usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            return out
        delta = choices[0].get("delta") or {}

        text = delta.get("content")
        if text:
            self.out_chars += len(text)
            if self.text_open is None:
                out += self._open_text()
            out += self.sse("content_block_delta",
                            {"type": "content_block_delta", "index": self.text_open,
                             "delta": {"type": "text_delta", "text": text}})
        # reasoning 增量不转发（保持 Anthropic 协议简单）

        for tc in delta.get("tool_calls") or []:
            oi = tc.get("index", 0)
            blk = self.tool_blocks.setdefault(oi, {"block": None, "id": None, "name": ""})
            fn = tc.get("function") or {}
            if blk["block"] is None:
                blk["block"] = self.next_index
                self.next_index += 1
                blk["id"] = tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"
                blk["name"] = fn.get("name", "")
                out += self.sse("content_block_start", {"type": "content_block_start", "index": blk["block"],
                                                        "content_block": {"type": "tool_use", "id": blk["id"],
                                                                          "name": blk["name"], "input": {}}})
            args = fn.get("arguments")
            if args:
                out += self.sse("content_block_delta", {"type": "content_block_delta", "index": blk["block"],
                                                        "delta": {"type": "input_json_delta", "partial_json": args}})
        return out

    def finish(self, finish_reason: str) -> bytes:
        out = b""
        if self.text_open is not None:
            out += self.sse("content_block_stop", {"type": "content_block_stop", "index": self.text_open})
        for oi in sorted(self.tool_blocks):
            out += self.sse("content_block_stop",
                            {"type": "content_block_stop", "index": self.tool_blocks[oi]["block"]})
        stop = stop_reason_of(finish_reason)
        out_tokens = (self.final_usage or {}).get("completion_tokens") or max(1, self.out_chars // 3)
        out += self.sse("message_delta", {"type": "message_delta",
                                          "delta": {"stop_reason": stop, "stop_sequence": None},
                                          "usage": {"output_tokens": out_tokens}})
        out += self.sse("message_stop", {"type": "message_stop"})
        return out


# ─────────────────────────── HTTP 服务 ───────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _anthropic_error(self, code: int, etype: str, message: str):
        self._json(code, {"type": "error", "error": {"type": etype, "message": message}})

    def do_GET(self):
        if self.path in ("/health", "/"):
            self._json(200, {"status": "ok", "upstream": UPSTAGE_BASE,
                             "models": {"big": BIG_MODEL, "small": SMALL_MODEL}})
        elif self.path.startswith("/v1/models"):
            # Anthropic 模型列表 API：Claude Code 启动时会查询校验
            now = "2025-01-01T00:00:00Z"
            data = [{"type": "model", "id": m, "display_name": n, "created_at": now}
                    for m, n in [("claude-sonnet-4-5", f"Sonnet (→{BIG_MODEL})"),
                                 ("claude-haiku-4-5", f"Haiku (→{SMALL_MODEL})"),
                                 ("solar-pro4", "Solar Pro 4"), ("solar-mini4", "Solar Mini 4")]]
            self._json(200, {"data": data, "has_more": False, "first_id": data[0]["id"],
                             "last_id": data[-1]["id"]})
        else:
            log(f"? 未处理的 GET {self.path}")
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._anthropic_error(400, "invalid_request_error", "请求体不是合法 JSON")

        path = self.path.split("?", 1)[0]  # Claude Code 会带 ?beta=true 之类的查询串
        if path.endswith("/count_tokens"):
            _, chars = convert_request(payload)
            return self._json(200, {"input_tokens": max(1, int(chars * 0.75) + 64)})

        if path.endswith("/responses"):
            return self.handle_responses(payload)

        if not path.endswith("/messages"):
            log(f"? 未处理的 POST {self.path}")
            return self._anthropic_error(404, "not_found_error", f"未知路径 {self.path}")

        requested_model = payload.get("model", "")
        try:
            upstream_payload, in_chars = convert_request(payload)
        except Exception as e:
            return self._anthropic_error(400, "invalid_request_error", f"请求转换失败: {e}")

        want_stream = bool(payload.get("stream"))
        if want_stream:
            upstream_payload["stream"] = True
            upstream_payload["stream_options"] = {"include_usage": True}
        est_input = max(1, int(in_chars * 0.75) + 64)
        t0 = time.time()
        log(f"→ {upstream_payload['model']} | msgs={len(upstream_payload['messages'])} "
            f"tools={len(upstream_payload.get('tools', []))} | stream={want_stream}")

        try:
            resp = requests.post(f"{UPSTAGE_BASE}/chat/completions",
                                 headers={"Authorization": f"Bearer {UPSTAGE_API_KEY}"},
                                 json=upstream_payload, stream=want_stream, timeout=(10, 600))
        except requests.RequestException as e:
            return self._anthropic_error(502, "api_error", f"上游连接失败: {e}")

        if resp.status_code != 200:
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text[:500]
            log(f"✗ 上游 {resp.status_code}: {str(detail)[:200]}")
            return self._anthropic_error(resp.status_code if resp.status_code < 500 else 502,
                                         "api_error", f"Upstage 错误: {detail}")

        if not want_stream:
            result = convert_response_nonstream(resp.json(), requested_model)
            log(f"✓ {time.time()-t0:.1f}s in={result['usage']['input_tokens']} "
                f"out={result['usage']['output_tokens']} stop={result['stop_reason']}")
            return self._json(200, result)

        # ── 流式 ──
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")  # SSE 结束即断，规避长度不确定
        self.end_headers()
        transformer = StreamTransformer(requested_model, est_input)
        finish = None
        n_lines, n_chunks, n_errs = 0, 0, 0
        try:
            resp.encoding = "utf-8"  # SSE 无 charset 声明，requests 默认 latin-1 会弄乱中文
            self.wfile.write(transformer.start())
            for raw in resp.iter_lines(decode_unicode=True):
                n_lines += 1
                if n_lines <= 2:
                    log(f"上游行样例: {raw!r}"[:150])
                if not raw or not raw.startswith("data:"):
                    continue
                data_str = raw[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    n_errs += 1
                    if n_errs <= 2:
                        log("JSON解析失败样例:", data_str[:150])
                    continue
                n_chunks += 1
                fr = (chunk.get("choices") or [{}])[0].get("finish_reason")
                if fr:
                    finish = fr
                piece = transformer.on_chunk(chunk)
                if piece:
                    self.wfile.write(piece)
                    self.wfile.flush()
            self.wfile.write(transformer.finish(finish or "stop"))
            self.wfile.flush()
            log(f"✓ {time.time()-t0:.1f}s 行={n_lines} 块={n_chunks} 解析错={n_errs} "
                f"out={transformer.final_usage and transformer.final_usage.get('completion_tokens')} stop={finish}")
        except (BrokenPipeError, ConnectionResetError):
            log("客户端断开")
        except Exception as e:
            log(f"✗ 流式处理异常: {e!r}")

    # ── Responses API 处理（Codex CLI 走这里）────────────────────
    def handle_responses(self, payload: dict):
        requested_model = payload.get("model", "")
        try:
            upstream_payload, in_chars = convert_responses_request(payload)
        except Exception as e:
            return self._json(400, {"error": {"message": f"请求转换失败: {e}", "type": "invalid_request_error"}})

        want_stream = bool(payload.get("stream"))
        if want_stream:
            upstream_payload["stream"] = True
            upstream_payload["stream_options"] = {"include_usage": True}
        est_input = max(1, int(in_chars * 0.75) + 64)
        t0 = time.time()
        log(f"→R {upstream_payload['model']} | msgs={len(upstream_payload['messages'])} "
            f"tools={len(upstream_payload.get('tools', []))} | stream={want_stream}")

        try:
            resp = requests.post(f"{UPSTAGE_BASE}/chat/completions",
                                 headers={"Authorization": f"Bearer {UPSTAGE_API_KEY}"},
                                 json=upstream_payload, stream=want_stream, timeout=(10, 600))
        except requests.RequestException as e:
            return self._json(502, {"error": {"message": f"上游连接失败: {e}", "type": "api_error"}})

        if resp.status_code != 200:
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text[:500]
            log(f"✗ 上游 {resp.status_code}: {str(detail)[:200]}")
            return self._json(resp.status_code if resp.status_code < 500 else 502,
                              {"error": {"message": f"Upstage 错误: {detail}", "type": "api_error"}})

        if not want_stream:
            chat = resp.json()
            msg = (chat.get("choices") or [{}])[0].get("message") or {}
            output = responses_output_items(msg)
            usage = responses_usage(chat.get("usage"), est_input,
                                    sum(len(i.get("content", [{}])[0].get("text", ""))
                                        for i in output if i.get("type") == "message"))
            log(f"✓ {time.time()-t0:.1f}s in={usage['input_tokens']} out={usage['output_tokens']}")
            return self._json(200, responses_response_object(requested_model, output, usage))

        # ── 流式 ──
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        transformer = ResponsesStreamTransformer(requested_model, est_input)
        finish = None
        try:
            resp.encoding = "utf-8"
            self.wfile.write(transformer.start())
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                data_str = raw[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                fr = (chunk.get("choices") or [{}])[0].get("finish_reason")
                if fr:
                    finish = fr
                piece = transformer.on_chunk(chunk)
                if piece:
                    self.wfile.write(piece)
                    self.wfile.flush()
            self.wfile.write(transformer.finish(finish or "stop"))
            self.wfile.flush()
            log(f"✓ {time.time()-t0:.1f}s ~in={est_input} "
                f"out={transformer.final_usage and transformer.final_usage.get('completion_tokens')} stop={finish}")
        except (BrokenPipeError, ConnectionResetError):
            log("客户端断开")
        except Exception as e:
            log(f"✗ Responses 流式异常: {e!r}")


# ─────────────────────────── Responses API（Codex CLI）───────────────────────────

def convert_responses_request(payload: dict):
    """OpenAI Responses 格式 → chat completions 格式。返回 (openai_payload, est_input_chars)"""
    msgs_out, char_count = [], 0
    id_map = {}

    def rid(tid):
        if not tid:
            return tid
        if tid.startswith("call_"):
            return tid
        if tid not in id_map:
            id_map[tid] = "call_" + uuid.uuid4().hex[:12]
        return id_map[tid]

    # instructions 相当于 system
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        msgs_out.append({"role": "system", "content": instructions})
        char_count += len(instructions)

    items = payload.get("input")
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": items}]}]

    for item in items or []:
        t = item.get("type", "message")
        if t == "message":
            role = item.get("role", "user")
            content = item.get("content")
            if isinstance(content, str):
                text = content
            else:
                text = "\n".join(p.get("text", "") for p in (content or [])
                                 if isinstance(p, dict) and p.get("type") in ("input_text", "output_text"))
            if role == "system" or role == "developer":
                msgs_out.append({"role": "system", "content": text})
            else:
                msgs_out.append({"role": role, "content": text})
            char_count += len(text)
        elif t == "function_call":
            msgs_out.append({"role": "assistant", "content": "",
                             "tool_calls": [{"id": rid(item.get("call_id")), "type": "function",
                                             "function": {"name": item.get("name", ""),
                                                          "arguments": item.get("arguments") or "{}"}}]})
        elif t == "function_call_output":
            out = item.get("output")
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False)
            msgs_out.append({"role": "tool", "tool_call_id": rid(item.get("call_id")), "content": out})
            char_count += len(out)
        # reasoning / local_shell_call 等块跳过（solar 不支持回放思考历史）

    out = {"model": map_model(payload.get("model", "")), "messages": msgs_out,
           "max_tokens": min(int(payload.get("max_output_tokens") or 8192), 32768)}
    if payload.get("temperature") is not None:
        out["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        out["top_p"] = payload["top_p"]

    reasoning = payload.get("reasoning") or {}
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        out["reasoning_effort"] = {"minimal": "low", "xhigh": "high"}.get(reasoning["effort"], reasoning["effort"])

    tools = []
    for t in payload.get("tools", []):
        if t.get("type") == "function":
            params = dict(t.get("parameters") or {})
            params.pop("$schema", None)
            tools.append({"type": "function",
                          "function": {"name": t.get("name", ""), "description": t.get("description", ""),
                                       "parameters": params}})
            char_count += len(json.dumps(t, ensure_ascii=False)) // 2
    if tools:
        out["tools"] = tools

    tc = payload.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") == "function":
        out["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
    elif tc in ("auto", "none", "required"):
        out["tool_choice"] = tc

    return out, char_count


def responses_output_items(message: dict):
    """chat completions 的 message → Responses output 数组"""
    items, content_parts = [], []
    if message.get("content"):
        content_parts.append({"type": "message", "id": "msg_" + uuid.uuid4().hex[:16], "role": "assistant",
                              "status": "completed",
                              "content": [{"type": "output_text", "text": message["content"], "annotations": []}]})
        items.append(content_parts[0])
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments") or "{}"
        try:
            json.loads(args)
        except json.JSONDecodeError:
            args = json.dumps({"_raw": args}, ensure_ascii=False)
        items.append({"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:16],
                      "call_id": tc.get("id") or ("call_" + uuid.uuid4().hex[:12]),
                      "name": fn.get("name", ""), "arguments": args, "status": "completed"})
    if not items:
        items.append({"type": "message", "id": "msg_" + uuid.uuid4().hex[:16], "role": "assistant",
                      "status": "completed",
                      "content": [{"type": "output_text", "text": "(空响应)", "annotations": []}]})
    return items


def responses_usage(chat_usage: dict, est_input: int, out_chars: int) -> dict:
    it = (chat_usage or {}).get("prompt_tokens") or est_input
    ot = (chat_usage or {}).get("completion_tokens") or max(1, out_chars // 3)
    return {"input_tokens": it, "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": ot, "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": it + ot}


def responses_response_object(model: str, output: list, usage: dict, status: str = "completed") -> dict:
    return {"id": "resp_" + uuid.uuid4().hex[:24], "object": "response", "created_at": int(time.time()),
            "status": status, "model": model, "output": output,
            "parallel_tool_calls": False, "previous_response_id": None, "reasoning": None,
            "store": False, "temperature": None, "tool_choice": "auto", "tools": [], "top_p": None,
            "usage": usage, "error": None, "incomplete_details": None, "metadata": {}}


class ResponsesStreamTransformer:
    """OpenAI chat chunk 流 → Responses API SSE 事件流"""

    def __init__(self, model: str, est_input: int):
        self.model = model
        self.resp_id = "resp_" + uuid.uuid4().hex[:24]
        self.est_input = est_input
        self.next_index = 0
        self.text = None       # {"item_id", "text"} 消息块打开时非 None
        self.calls = {}        # chat tool index → {"item_id","call_id","name","args","out_index"}
        self.final_usage = None
        self.out_chars = 0

    def sse(self, ev: dict) -> bytes:
        data = json.dumps(ev, ensure_ascii=False)
        return f"event: {ev['type']}\ndata: {data}\n\n".encode()

    def _resp_skeleton(self, status="in_progress"):
        return {"id": self.resp_id, "object": "response", "created_at": int(time.time()),
                "status": status, "model": self.model, "output": [], "parallel_tool_calls": False,
                "previous_response_id": None, "reasoning": None, "store": False, "temperature": None,
                "tool_choice": "auto", "tools": [], "top_p": None, "usage": None,
                "error": None, "incomplete_details": None, "metadata": {}}

    def start(self) -> bytes:
        return self.sse({"type": "response.created", "response": self._resp_skeleton()})

    def on_chunk(self, chunk: dict) -> bytes:
        out = b""
        if chunk.get("usage"):
            self.final_usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            return out
        delta = choices[0].get("delta") or {}

        text = delta.get("content")
        if text:
            self.out_chars += len(text)
            if self.text is None:
                item_id = "msg_" + uuid.uuid4().hex[:16]
                self.text = {"item_id": item_id, "text": ""}
                oi = self.next_index
                self.next_index += 1
                self.text["out_index"] = oi
                out += self.sse({"type": "response.output_item.added", "output_index": oi,
                                 "item": {"type": "message", "id": item_id, "role": "assistant",
                                          "status": "in_progress", "content": []}})
                out += self.sse({"type": "response.content_part.added", "item_id": item_id,
                                 "output_index": oi, "content_index": 0,
                                 "part": {"type": "output_text", "text": "", "annotations": []}})
            self.text["text"] += text
            out += self.sse({"type": "response.output_text.delta", "item_id": self.text["item_id"],
                             "output_index": self.text["out_index"], "content_index": 0, "delta": text})

        for tc in delta.get("tool_calls") or []:
            ci = tc.get("index", 0)
            call = self.calls.setdefault(ci, {"item_id": None, "call_id": None, "name": "",
                                              "args": "", "out_index": None})
            fn = tc.get("function") or {}
            if call["out_index"] is None:
                call["item_id"] = "fc_" + uuid.uuid4().hex[:16]
                call["call_id"] = tc.get("id") or ("call_" + uuid.uuid4().hex[:12])
                call["name"] = fn.get("name", "")
                call["out_index"] = self.next_index
                self.next_index += 1
                out += self.sse({"type": "response.output_item.added", "output_index": call["out_index"],
                                 "item": {"type": "function_call", "id": call["item_id"],
                                          "call_id": call["call_id"], "name": call["name"],
                                          "arguments": "", "status": "in_progress"}})
            args = fn.get("arguments")
            if args:
                call["args"] += args
                out += self.sse({"type": "response.function_call_arguments.delta",
                                 "item_id": call["item_id"], "output_index": call["out_index"], "delta": args})
        return out

    def finish(self, finish_reason: str) -> bytes:
        out = b""
        output = []
        if self.text is not None:
            msg_item = {"type": "message", "id": self.text["item_id"], "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": self.text["text"], "annotations": []}]}
            out += self.sse({"type": "response.output_text.done", "item_id": self.text["item_id"],
                             "output_index": self.text["out_index"], "content_index": 0,
                             "text": self.text["text"]})
            out += self.sse({"type": "response.output_item.done",
                             "output_index": self.text["out_index"], "item": msg_item})
            output.append(msg_item)
        for ci in sorted(self.calls):
            c = self.calls[ci]
            fc_item = {"type": "function_call", "id": c["item_id"], "call_id": c["call_id"],
                       "name": c["name"], "arguments": c["args"] or "{}", "status": "completed"}
            out += self.sse({"type": "response.function_call_arguments.done", "item_id": c["item_id"],
                             "output_index": c["out_index"], "arguments": fc_item["arguments"]})
            out += self.sse({"type": "response.output_item.done",
                             "output_index": c["out_index"], "item": fc_item})
            output.append(fc_item)
        usage = responses_usage(self.final_usage, self.est_input, self.out_chars)
        resp = self._resp_skeleton("completed" if finish_reason != "length" else "incomplete")
        resp["output"] = output
        resp["usage"] = usage
        if finish_reason == "length":
            resp["incomplete_details"] = {"reason": "max_output_tokens"}
        out += self.sse({"type": "response.completed", "response": resp})
        return out


def main():
    if not UPSTAGE_API_KEY:
        print("错误: 未设置 UPSTAGE_API_KEY 环境变量（在 https://console.upstage.ai 获取）")
        print('  用法: UPSTAGE_API_KEY=up_xxx python3 proxy.py')
        sys.exit(1)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print(f"solar-proxy 已启动: http://127.0.0.1:{PORT}")
    print(f"  主模型 → {BIG_MODEL}   轻量模型(haiku槽位) → {SMALL_MODEL}")
    print(f"  上游: {UPSTAGE_BASE}")
    print("Claude Code 接法 (Anthropic 协议 /v1/messages):")
    print(f'  export ANTHROPIC_BASE_URL=http://127.0.0.1:{PORT}')
    print('  export ANTHROPIC_AUTH_TOKEN=local-proxy')
    print('  export ANTHROPIC_MODEL=solar-pro4')
    print("Codex CLI 接法 (Responses 协议 /v1/responses) — ~/.codex/config.toml:")
    print('  model = "solar-pro4"')
    print('  model_provider = "upstage"')
    print('  [model_providers.upstage]')
    print('  name = "Upstage"')
    print(f'  base_url = "http://127.0.0.1:{PORT}/v1"')
    print('  env_key = "UPSTAGE_API_KEY"')
    print('  wire_api = "responses"')
    server.serve_forever()


if __name__ == "__main__":
    main()
