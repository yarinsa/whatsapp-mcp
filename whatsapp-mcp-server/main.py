from typing import List, Dict, Any, Optional
from mcp.server.mcpserver import MCPServer
from whatsapp import (
    search_contacts as whatsapp_search_contacts,
    list_messages as whatsapp_list_messages,
    list_chats as whatsapp_list_chats,
    get_chat as whatsapp_get_chat,
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
    get_contact_chats as whatsapp_get_contact_chats,
    get_last_interaction as whatsapp_get_last_interaction,
    get_message_context as whatsapp_get_message_context,
    send_message as whatsapp_send_message,
    send_file as whatsapp_send_file,
    send_audio_message as whatsapp_audio_voice_message,
    download_media as whatsapp_download_media,
    mark_chat as whatsapp_mark_chat
)
from search import search_messages as whatsapp_search_messages

import dataclasses
import re
import functools
from datetime import datetime
import os
import sys

from bridge import bridge_status as probe_bridge, require_bridge

# MCP_TRANSPORT=streamable-http serves the tools over HTTPS behind OAuth (for
# remote use from claude.ai / the Claude iPhone app); the default stays stdio.
TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")

if TRANSPORT == "streamable-http":
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from auth import SCOPE, WhatsAppOAuthProvider

    PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")  # e.g. https://whatsapp-mcp.up.railway.app
    oauth = WhatsAppOAuthProvider(PUBLIC_URL, os.environ["WHATSAPP_OWNER_PHONE"])
    mcp = MCPServer(
        "whatsapp",
        auth_server_provider=oauth,
        auth=AuthSettings(
            issuer_url=PUBLIC_URL,
            resource_server_url=f"{PUBLIC_URL}/mcp",
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
            validate_token_resource=True,
        ),
    )
    mcp.custom_route("/login", methods=["GET", "POST"])(oauth.login_page)
else:
    mcp = MCPServer("whatsapp")


def plain(value: Any) -> Any:
    """Turn the dataclasses the query layer returns (Chat, Contact, Message...)
    into JSON-ready dicts. MCP SDK 2 validates tool output against the declared
    return type, so a dataclass where a dict is declared fails the call."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def needs_bridge(fn):
    """Fail fast with a clear message when the bridge is down, stale or not ours."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        require_bridge()
        return plain(fn(*args, **kwargs))
    return wrapper


@mcp.tool()
def bridge_status() -> Dict[str, Any]:
    """Check the WhatsApp bridge: whether it is running, connected, and when the last message synced.

    Call this first when other WhatsApp tools fail or return surprisingly little.
    """
    return probe_bridge()

@mcp.tool()
@needs_bridge
def search_contacts(query: str) -> List[Dict[str, Any]]:
    """Search WhatsApp contacts and groups by name (Hebrew or English) or phone number.

    Groups come last, with is_group=true and their group JID.

    Args:
        query: Search term to match against contact/group names or phone numbers
    """
    contacts = whatsapp_search_contacts(query)
    return contacts

@mcp.tool()
@needs_bridge
def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: Optional[bool] = None,
    context_before: int = 1,
    context_after: int = 1
) -> str:
    """Get WhatsApp messages matching specified criteria with optional context.

    For keyword search prefer search_messages (indexed, compact output).
    
    Args:
        after: Optional ISO-8601 formatted string to only return messages after this date
        before: Optional ISO-8601 formatted string to only return messages before this date
        sender_phone_number: Optional sender: phone, LID, JID or name; matches all of the person's identities
        chat_jid: Optional chat JID to filter messages by chat
        query: Optional search words; all must appear (any order)
        limit: Maximum number of messages to return (default 20)
        page: Page number for pagination (default 0)
        include_context: Include messages around each match (default: only when query is given)
        context_before: Number of messages to include before each match (default 1)
        context_after: Number of messages to include after each match (default 1)
    """
    messages = whatsapp_list_messages(
        after=after,
        before=before,
        sender_phone_number=sender_phone_number,
        chat_jid=chat_jid,
        query=query,
        limit=limit,
        page=page,
        include_context=include_context,
        context_before=context_before,
        context_after=context_after
    )
    return messages

@mcp.tool()
@needs_bridge
def search_messages(
    query: Optional[str] = None,
    chat_jid: Optional[str] = None,
    sender: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    from_me: Optional[bool] = None,
    media_type: Optional[str] = None,
    sort: str = "recent",
    limit: int = 30,
    page: int = 0,
) -> str:
    """Full-text search across all WhatsApp messages (indexed, Hebrew and English).

    Every word must appear, in any order, as a substring (so Hebrew prefixes
    like ו/ה/ב/ל still match). Returns one line per hit: date, chat, sender,
    snippet, and the message id + chat JID for get_message_context.

    Args:
        query: Words to find; optional if a filter is given
        chat_jid: Limit to one chat
        sender: Person by name, phone, LID or JID (all their identities match)
        after: ISO date/time, inclusive
        before: ISO date/time, exclusive
        from_me: True for only my messages, False for only others'
        media_type: image, video, audio or document
        sort: "recent" (default) or "relevance"
        limit: Max results (default 30)
        page: Page number
    """
    return whatsapp_search_messages(query, chat_jid, sender, after, before, from_me, media_type, sort, limit, page)

@mcp.tool()
@needs_bridge
def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
    unread_only: bool = False
) -> List[Dict[str, Any]]:
    """Get WhatsApp chats matching specified criteria.
    
    Args:
        query: Optional search term to filter chats by name or JID
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
        include_last_message: Whether to include the last message in each chat (default True)
        sort_by: Field to sort results by, either "last_active" or "name" (default "last_active")
        unread_only: Only return chats with unread messages or marked unread (default False)

    Each chat includes unread_count and marked_unread.
    """
    chats = whatsapp_list_chats(
        query=query,
        limit=limit,
        page=page,
        include_last_message=include_last_message,
        sort_by=sort_by,
        unread_only=unread_only
    )
    return chats

@mcp.tool()
@needs_bridge
def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Dict[str, Any]]:
    """Get WhatsApp chat metadata by JID.
    
    Args:
        chat_jid: The JID of the chat to retrieve
        include_last_message: Whether to include the last message (default True)
    """
    chat = whatsapp_get_chat(chat_jid, include_last_message)
    return chat

@mcp.tool()
@needs_bridge
def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Dict[str, Any]]:
    """Get WhatsApp chat metadata by sender phone number.
    
    Args:
        sender_phone_number: The phone number to search for
    """
    chat = whatsapp_get_direct_chat_by_contact(sender_phone_number)
    return chat

@mcp.tool()
@needs_bridge
def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Dict[str, Any]]:
    """Get all WhatsApp chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    chats = whatsapp_get_contact_chats(jid, limit, page)
    return chats

@mcp.tool()
@needs_bridge
def get_last_interaction(jid: str) -> str:
    """Get most recent WhatsApp message involving the contact.
    
    Args:
        jid: The JID of the contact to search for
    """
    message = whatsapp_get_last_interaction(jid)
    return message

@mcp.tool()
@needs_bridge
def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5,
    chat_jid: Optional[str] = None,
) -> Dict[str, Any]:
    """Get context around a specific WhatsApp message.

    Args:
        message_id: The ID of the message to get context for
        before: Number of messages to include before the target message (default 5)
        after: Number of messages to include after the target message (default 5)
        chat_jid: Chat the message is in (recommended: IDs are only unique per chat)
    """
    context = whatsapp_get_message_context(message_id, before, after, chat_jid)
    return context

@mcp.tool()
@needs_bridge
def send_message(recipient: str, message: str) -> Dict[str, Any]:
    """Send a WhatsApp text message to a person or group.

    Args:
        recipient: Who gets it, as "Name (jid)" with the exact name and JID from list_chats
            or search_contacts, e.g. "Dana Levi (972501234567@s.whatsapp.net)" or
            "Family (120363000000000000@g.us)". For a number with no chat yet use
            "Name (972501234567)". The user reviews this before approving, and a name
            that does not match the chat is refused.
        message: The message text to send
    """
    jid, err = _resolve_chat(recipient)
    if err:
        return {"success": False, "message": err}
    success, status_message = whatsapp_send_message(jid, message)
    return {"success": success, "message": status_message}

@mcp.tool()
@needs_bridge
def send_file(recipient: str, media_path: str) -> Dict[str, Any]:
    """Send a file (picture, video, document or raw audio) to a person or group.

    Args:
        recipient: Who gets it, as "Name (jid)" with the exact name and JID from list_chats
            or search_contacts, e.g. "Dana Levi (972501234567@s.whatsapp.net)" or
            "Family (120363000000000000@g.us)". For a number with no chat yet use
            "Name (972501234567)". The user reviews this before approving, and a name
            that does not match the chat is refused.
        media_path: Absolute path to the file to send
    """
    jid, err = _resolve_chat(recipient)
    if err:
        return {"success": False, "message": err}
    success, status_message = whatsapp_send_file(jid, media_path)
    return {"success": success, "message": status_message}

@mcp.tool()
@needs_bridge
def send_audio_message(recipient: str, media_path: str) -> Dict[str, Any]:
    """Send an audio file as a WhatsApp voice message. If it fails because ffmpeg
    is missing, use send_file instead.

    Args:
        recipient: Who gets it, as "Name (jid)" with the exact name and JID from list_chats
            or search_contacts, e.g. "Dana Levi (972501234567@s.whatsapp.net)" or
            "Family (120363000000000000@g.us)". For a number with no chat yet use
            "Name (972501234567)". The user reviews this before approving, and a name
            that does not match the chat is refused.
        media_path: Absolute path to the audio file (converted to Opus .ogg if needed)
    """
    jid, err = _resolve_chat(recipient)
    if err:
        return {"success": False, "message": err}
    success, status_message = whatsapp_audio_voice_message(jid, media_path)
    return {"success": success, "message": status_message}

@mcp.tool()
@needs_bridge
def download_media(message_id: str, chat_jid: str) -> Dict[str, Any]:
    """Download media from a WhatsApp message and get the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        A dictionary containing success status, a status message, and the file path if successful
    """
    file_path = whatsapp_download_media(message_id, chat_jid)
    
    if file_path:
        return {
            "success": True,
            "message": "Media downloaded successfully",
            "file_path": file_path
        }
    else:
        return {
            "success": False,
            "message": "Failed to download media"
        }

def _is_group(jid: str) -> bool:
    # Groups and communities (a community's announcement group is also @g.us).
    return jid.endswith("@g.us")


_CHAT_ENTRY = re.compile(r"^\s*(?P<name>.*?)\s*\((?P<jid>[^()\s]+@[\w.]+|\d{6,})\)\s*(?:[-–—:·|]\s*(?P<summary>.*\S))?\s*$")
_ENTRY_FORMAT = 'Use the format "Chat name (jid)", e.g. "Dana Levi (972501234567@s.whatsapp.net)"'


def _resolve_chat(entry: str):
    """Turn a "Name (jid)" entry into a JID, checking the name against the stored chat.

    Approval prompts show these entries, so the name must really belong to the JID.
    A chat that isn't stored yet (e.g. a new phone number) is accepted as given.
    Returns (jid, None) on success or (None, error message).
    """
    name, jid = _parse_chat_entry(entry or "")
    if not jid:
        return None, _ENTRY_FORMAT
    lookup = jid if "@" in jid else f"{jid}@s.whatsapp.net"
    chat = whatsapp_get_chat(lookup, include_last_message=False)
    actual = (chat.name if chat else None) or ""
    if actual and name.casefold() != actual.casefold():
        return None, f"Name does not match this chat (stored name: {actual}). Nothing was done; retry with the stored name."
    return jid, None


def _parse_chat_entry(entry: str):
    """Split "Name (jid)" into (name, jid). Returns (None, None) if malformed."""
    m = _CHAT_ENTRY.match(entry)
    return (m["name"], m["jid"]) if m else (None, None)


_SUMMARY_FORMAT = ('Add a short summary after the JID so the user sees what they approve, e.g. '
                   '"Dana Levi (972501234567@s.whatsapp.net) — 3 unread from Dana: \'Are you coming tonight?…\'"')


def _mark_many_read(chats: List[str], want_groups: bool) -> Dict[str, Any]:
    kind = "group/community" if want_groups else "direct"
    results = []
    for entry in dict.fromkeys(chats):  # dedupe, keep order
        m = _CHAT_ENTRY.match(entry or "")
        if m and not m["summary"]:
            results.append({"chat": entry, "success": False, "message": _SUMMARY_FORMAT})
            continue
        jid, err = _resolve_chat(entry)
        if err:
            results.append({"chat": entry, "success": False, "message": err})
            continue
        if _is_group(jid) != want_groups:
            results.append({"chat": entry, "success": False,
                            "message": f"Not a {kind} chat; use mark_{'direct' if want_groups else 'group'}_chats_read"})
            continue
        ok, msg = whatsapp_mark_chat(jid, read=True)
        results.append({"chat": entry, "success": ok, "message": msg})
    done = sum(r["success"] for r in results)
    return {"success": done == len(results), "marked": done, "failed": len(results) - done, "results": results}


@mcp.tool()
@needs_bridge
def mark_direct_chats_read(chats: List[str]) -> Dict[str, Any]:
    """Mark one or more direct (1:1) WhatsApp chats as read. The other person sees
    blue ticks, and the unread badge clears on your phone and other devices.
    Group or community chats are refused; use mark_group_chats_read for those.
    Receipts go only to messages stored on this server; the badge clears either way.

    Args:
        chats: One entry per chat as "Chat name (jid) — summary", using the exact
            name and JID from list_chats. The summary is what the user approves, so make it
            concrete: unread count, who wrote, and a truncated quote of the latest message,
            e.g. ["Dana Levi (972501234567@s.whatsapp.net) — 3 unread from Dana: 'Are you coming tonight?…'"].
            The user reviews these names before approving, and a name that does not
            match the chat is refused.
    """
    return _mark_many_read(chats, want_groups=False)


@mcp.tool()
@needs_bridge
def mark_group_chats_read(chats: List[str]) -> Dict[str, Any]:
    """Mark one or more WhatsApp groups or communities as read. Members may see your
    read receipt, and the unread badge clears on your phone and other devices.
    Direct chats are refused; use mark_direct_chats_read for those.

    Args:
        chats: One entry per group as "Group name (jid) — summary", using the exact
            name and JID from list_chats. The summary is what the user approves, so make it
            concrete: unread count, who wrote, and a truncated quote of the latest message,
            e.g. ["Family (120363000000000000@g.us) — 12 unread from Mom, Avi, Noa: 'Who brings dessert?…'"].
            The user reviews these names before approving, and a name that does not
            match the chat is refused.
    """
    return _mark_many_read(chats, want_groups=True)


@mcp.tool()
@needs_bridge
def mark_chat_unread(chat: str) -> Dict[str, Any]:
    """Mark a WhatsApp chat as unread on all your devices (a reminder to come back to it).
    It does not undo read receipts already sent.

    Args:
        chat: The chat as "Name (jid)" with the exact name and JID from list_chats,
            e.g. "Dana Levi (972501234567@s.whatsapp.net)"
    """
    jid, err = _resolve_chat(chat)
    if err:
        return {"success": False, "message": err}
    success, message = whatsapp_mark_chat(jid, read=False)
    return {"success": success, "message": message}


if __name__ == "__main__":
    status = probe_bridge()
    print(f"[whatsapp-mcp] bridge check: {status['summary']}", file=sys.stderr)
    if TRANSPORT == "streamable-http":
        from urllib.parse import urlparse
        from mcp.server.transport_security import TransportSecuritySettings

        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8000")),
            transport_security=TransportSecuritySettings(allowed_hosts=[urlparse(PUBLIC_URL).netloc]),
        )
    else:
        mcp.run(transport="stdio")