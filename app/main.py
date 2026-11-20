import os
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlencode

import google.auth
from google.auth.transport.requests import Request
import requests
from fastapi import BackgroundTasks, FastAPI, Request as FastAPIRequest
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI(title="Gemini Enterprise Google Chat Bot")

# Required environment variables
GE_PROJECT = os.environ["GE_PROJECT"]
GE_LOCATION = os.environ.get("GE_LOCATION", "global")
GE_APP_ID = os.environ["GE_APP_ID"]
OAUTH_CLIENT_ID = os.environ["OAUTH_CLIENT_ID"]
OAUTH_CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]
SERVICE_URL = os.environ["SERVICE_URL"].rstrip("/")

# Optional: set GE_AGENT_ID to route to a specific custom agent instead of default_assistant
GE_AGENT_ID = os.environ.get("GE_AGENT_ID", "").strip()
ENABLE_WEB_GROUNDING = os.environ.get("ENABLE_WEB_GROUNDING", "false").lower() == "true"

SCOPES = "https://www.googleapis.com/auth/discoveryengine.assist.readwrite"

# In-memory stores for rapid prototyping (deploy with --max-instances=1).
# For multi-instance production deployments, replace with Firestore or Redis.
USER_TOKENS: Dict[str, dict] = {}
CHAT_SESSIONS: Dict[Tuple[str, str], str] = {}


def get_bot_token() -> str:
    """Returns an OAuth access token for the Chat bot service account."""
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/chat.bot"])
    creds.refresh(Request())
    return creds.token


def get_user_ge_token(user_name: str) -> Optional[str]:
    """Returns a valid per-user OAuth token, refreshing automatically when needed."""
    info = USER_TOKENS.get(user_name)
    if not info:
        return None

    if time.time() >= info["expires_at"] - 60 and info.get("refresh_token"):
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET,
                "refresh_token": info["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        if resp.ok:
            data = resp.json()
            info["access_token"] = data["access_token"]
            info["expires_at"] = time.time() + data.get("expires_in", 3600)
        else:
            USER_TOKENS.pop(user_name, None)
            return None

    return info["access_token"]


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/login")
async def login(user: str):
    """Starts the OAuth 2.0 consent flow for a Google Chat user."""
    params = urlencode(
        {
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": f"{SERVICE_URL}/callback",
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": user,
        }
    )
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@app.get("/callback")
async def callback(code: str, state: str):
    """Handles the OAuth 2.0 callback and stores the user's token."""
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
            "redirect_uri": f"{SERVICE_URL}/callback",
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if not resp.ok:
        return HTMLResponse(f"<h3>Authentication failed: {resp.text}</h3>", status_code=400)

    data = resp.json()
    USER_TOKENS[state] = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": time.time() + data.get("expires_in", 3600),
    }
    return HTMLResponse(
        "<h2>✅ Gemini Enterprise 로그인 완료! (Authentication Successful)</h2>"
        "<p>이 창을 닫고 Google Chat으로 돌아가서 다시 질문해 주세요.<br/>"
        "You can close this tab and return to Google Chat.</p>"
    )


def send_chat_message(
    space_name: str, text: str, private_user: Optional[str] = None
) -> requests.Response:
    """Posts a message to Google Chat, optionally visible only to private_user."""
    chat_token = get_bot_token()
    body = {"text": text}
    if private_user:
        body["privateMessageViewer"] = {"name": private_user}

    return requests.post(
        f"https://chat.googleapis.com/v1/{space_name}/messages",
        headers={
            "Authorization": f"Bearer {chat_token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )


def call_ge_and_reply(
    space_name: str, user_name: str, user_text: str, is_private: bool
) -> None:
    """Calls Gemini Enterprise streamAssist asynchronously and posts the reply."""
    # 1) Prompt user to sign in once if no OAuth token is stored yet
    ge_token = get_user_ge_token(user_name)
    if not ge_token:
        login_url = f"{SERVICE_URL}/login?{urlencode({'user': user_name})}"
        send_chat_message(
            space_name,
            "🔐 개인 Google Drive/Workspace 데이터 검색을 위해 최초 1회 로그인이 필요합니다.\n"
            f"👉 {login_url}",
            private_user=user_name if is_private else None,
        )
        return

    # 2) Post an immediate loading indicator in public mode (private messages cannot be PATCHed)
    msg_name = None
    if not is_private:
        init_resp = send_chat_message(
            space_name, "🔍 Gemini Enterprise에서 찾아보는 중..."
        )
        if init_resp.ok:
            msg_name = init_resp.json().get("name")

    # 3) Call Gemini Enterprise streamAssist API
    session_key = (space_name, user_name)
    existing_session = CHAT_SESSIONS.get(session_key)

    try:
        host = (
            "discoveryengine.googleapis.com"
            if GE_LOCATION == "global"
            else f"{GE_LOCATION}-discoveryengine.googleapis.com"
        )
        url = (
            f"https://{host}/v1alpha/projects/{GE_PROJECT}/locations/{GE_LOCATION}"
            f"/collections/default_collection/engines/{GE_APP_ID}"
            f"/assistants/default_assistant:streamAssist"
        )

        tools_spec: Dict[str, dict] = {"vertexAiSearchSpec": {}}
        if ENABLE_WEB_GROUNDING:
            tools_spec["webGroundingSpec"] = {}

        payload: Dict[str, object] = {
            "session": existing_session,
            "query": {"text": user_text},
            "toolsSpec": tools_spec,
        }
        if GE_AGENT_ID:
            payload["agentsSpec"] = {"agentSpecs": [{"agentId": GE_AGENT_ID}]}

        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {ge_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=180,
        )
        resp.encoding = "utf-8"

        if resp.status_code != 200:
            print("GE ERROR:", resp.status_code, resp.text[:500])
            answer_text = f"응답을 가져오지 못했습니다. (HTTP {resp.status_code})"
        else:
            events = resp.json()
            if isinstance(events, dict):
                events = [events]

            answer_text = ""
            for event in events:
                # Save GE-managed session resource name for multi-turn continuity
                session_info = event.get("sessionInfo") or {}
                if session_info.get("session"):
                    CHAT_SESSIONS[session_key] = session_info["session"]

                for reply in event.get("answer", {}).get("replies", []):
                    content = reply.get("groundedContent", {}).get("content", {})
                    if content and not content.get("thought"):
                        answer_text += content.get("text", "")

            if not answer_text:
                answer_text = "답변 내용이 비어 있습니다."
    except Exception as exc:
        print("GE exception:", exc)
        answer_text = f"요청 처리 중 오류가 발생했습니다: {exc}"

    # 4) Deliver final response
    if msg_name and not is_private:
        chat_token = get_bot_token()
        requests.patch(
            f"https://chat.googleapis.com/v1/{msg_name}",
            headers={
                "Authorization": f"Bearer {chat_token}",
                "Content-Type": "application/json",
            },
            params={"updateMask": "text"},
            json={"text": answer_text},
            timeout=30,
        )
    else:
        send_chat_message(
            space_name,
            answer_text,
            private_user=user_name if is_private else None,
        )


@app.post("/")
async def handle_chat_event(req: FastAPIRequest, background_tasks: BackgroundTasks):
    """Receives Google Chat HTTP events and delegates execution asynchronously."""
    event = await req.json()

    # Supports both Google Workspace Add-on and legacy Google Chat event payloads
    payload = (
        event.get("chat", {}).get("messagePayload")
        or event.get("chat", {}).get("appCommandPayload")
        or event
    )
    message = payload.get("message", {})
    space = payload.get("space", {})
    space_name = space.get("name")
    space_type = space.get("type", "")
    user_name = (
        message.get("sender", {}).get("name")
        or event.get("chat", {}).get("user", {}).get("name", "")
    )
    raw_text = (message.get("argumentText") or message.get("text") or "").strip()

    if not space_name or not user_name:
        return {}

    # Optional /reset command to clear multi-turn GE session
    if raw_text.lower() in ("/reset", "reset"):
        CHAT_SESSIONS.pop((space_name, user_name), None)
        background_tasks.add_task(
            send_chat_message,
            space_name,
            "🔄 대화 세션을 초기화했습니다. 새로운 질문을 입력해 주세요.",
            user_name if space_type != "DM" else None,
        )
        return {}

    # Dual visibility mode: /private slash command -> private; @mention -> public
    has_slash = bool(message.get("slashCommand") or payload.get("appCommandMetadata"))
    is_private_req = has_slash or raw_text.startswith("/private")
    user_text = raw_text.removeprefix("/private").strip()

    # In 1:1 DMs, messages are already private; privateMessageViewer applies only to Spaces
    is_private = is_private_req and (space_type != "DM")

    if not user_text:
        return {}

    background_tasks.add_task(
        call_ge_and_reply, space_name, user_name, user_text, is_private
    )
    # Return empty JSON immediately to satisfy Google Chat's 30s webhook timeout cleanly
    return {}
