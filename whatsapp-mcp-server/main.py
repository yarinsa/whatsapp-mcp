from typing import List, Dict, Any, Optional
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
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
    mark_chat as whatsapp_mark_chat,
    leave_group as whatsapp_leave_group,
    fetch_history as whatsapp_fetch_history,
    sync_unread as whatsapp_sync_unread,
    unread_state as whatsapp_unread_state,
    list_channels as whatsapp_list_channels,
    unfollow_channel as whatsapp_unfollow_channel,
    get_lists as whatsapp_get_lists,
    create_list as whatsapp_create_list,
    edit_list as whatsapp_edit_list,
    set_list_chats as whatsapp_set_list_chats
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


_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
_DESTRUCTIVE = ToolAnnotations(destructiveHint=True, readOnlyHint=False, idempotentHint=False, openWorldHint=True)


@mcp.tool(annotations=_READ)
def bridge_status() -> Dict[str, Any]:
    """Check the WhatsApp bridge: whether it is running, connected, and when the last message synced.

    Call this first when other WhatsApp tools fail or return surprisingly little.
    """
    status = probe_bridge()
    if isinstance(status, dict) and status.get("connected"):
        status.update(whatsapp_unread_state())
    return status

@mcp.tool(annotations=_READ)
@needs_bridge
def search_contacts(query: str) -> List[Dict[str, Any]]:
    """Search WhatsApp contacts and groups by name (Hebrew or English) or phone number.

    Groups come last, with is_group=true and their group JID.

    Args:
        query: Search term to match against contact/group names or phone numbers
    """
    contacts = whatsapp_search_contacts(query)
    return contacts

@mcp.tool(annotations=_READ)
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

@mcp.tool(annotations=_READ)
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

@mcp.tool(annotations=_READ)
@needs_bridge
def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
    unread_only: bool = False
) -> Any:
    """Get WhatsApp chats matching specified criteria.
    
    Args:
        query: Optional search term to filter chats by name or JID
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
        include_last_message: Whether to include the last message in each chat (default True)
        sort_by: Field to sort results by, either "last_active" or "name" (default "last_active")
        unread_only: Only return chats with unread messages or marked unread (default False)

    Each chat includes unread_count and marked_unread. With unread_only=True the result
    is {"chats": [...], "phone_unread_total", "store_unread_total", "drift"}; when drift
    is true, call sync_unread first so messages missed while the bridge was down appear.
    """
    chats = whatsapp_list_chats(
        query=query,
        limit=limit,
        page=page,
        include_last_message=include_last_message,
        sort_by=sort_by,
        unread_only=unread_only
    )
    if unread_only:
        return {"chats": chats, **whatsapp_unread_state()}
    return chats

@mcp.tool(annotations=_READ)
@needs_bridge
def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Dict[str, Any]]:
    """Get WhatsApp chat metadata by JID.
    
    Args:
        chat_jid: The JID of the chat to retrieve
        include_last_message: Whether to include the last message (default True)
    """
    chat = whatsapp_get_chat(chat_jid, include_last_message)
    return chat

@mcp.tool(annotations=_READ)
@needs_bridge
def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Dict[str, Any]]:
    """Get WhatsApp chat metadata by sender phone number.
    
    Args:
        sender_phone_number: The phone number to search for
    """
    chat = whatsapp_get_direct_chat_by_contact(sender_phone_number)
    return chat

@mcp.tool(annotations=_READ)
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

@mcp.tool(annotations=_READ)
@needs_bridge
def get_last_interaction(jid: str) -> str:
    """Get most recent WhatsApp message involving the contact.
    
    Args:
        jid: The JID of the contact to search for
    """
    message = whatsapp_get_last_interaction(jid)
    return message

@mcp.tool(annotations=_READ)
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

def _with_mark_read(jid: str, success: bool, status_message: str, mark_read: bool) -> Dict[str, Any]:
    """Build a send result, marking the chat read afterwards when asked and the send worked."""
    result: Dict[str, Any] = {"success": success, "message": status_message}
    if success and mark_read:
        read_ok, read_msg = whatsapp_mark_chat(jid, True)
        result["marked_read"] = read_ok
        if not read_ok:
            result["mark_read_error"] = read_msg
    return result

@mcp.tool(annotations=_WRITE)
@needs_bridge
def send_message(recipient: str, message: str, mark_read: bool = False) -> Dict[str, Any]:
    """Send a WhatsApp text message to a person or group.

    When replying to unread messages, pass mark_read=True to also mark the chat
    read in the same call, instead of calling a mark-read tool afterwards.

    Args:
        recipient: Who gets it, as "Name (jid)" with the exact name and JID from list_chats
            or search_contacts, e.g. "Dana Levi (972501234567@s.whatsapp.net)" or
            "Family (120363000000000000@g.us)". For a number with no chat yet use
            "Name (972501234567)". The user reviews this before approving, and a name
            that does not match the chat is refused.
        message: The message text to send
        mark_read: Also mark the chat read (sending read receipts) after the message is sent
    """
    jid, err = _resolve_chat(recipient)
    if err:
        return {"success": False, "message": err}
    success, status_message = whatsapp_send_message(jid, message)
    return _with_mark_read(jid, success, status_message, mark_read)

@mcp.tool(annotations=_WRITE)
@needs_bridge
def send_file(recipient: str, media_path: str, mark_read: bool = False) -> Dict[str, Any]:
    """Send a file (picture, video, document or raw audio) to a person or group.

    Args:
        recipient: Who gets it, as "Name (jid)" with the exact name and JID from list_chats
            or search_contacts, e.g. "Dana Levi (972501234567@s.whatsapp.net)" or
            "Family (120363000000000000@g.us)". For a number with no chat yet use
            "Name (972501234567)". The user reviews this before approving, and a name
            that does not match the chat is refused.
        media_path: Absolute path to the file to send
        mark_read: Also mark the chat read (sending read receipts) after it is sent
    """
    jid, err = _resolve_chat(recipient)
    if err:
        return {"success": False, "message": err}
    success, status_message = whatsapp_send_file(jid, media_path)
    return _with_mark_read(jid, success, status_message, mark_read)

@mcp.tool(annotations=_WRITE)
@needs_bridge
def send_audio_message(recipient: str, media_path: str, mark_read: bool = False) -> Dict[str, Any]:
    """Send an audio file as a WhatsApp voice message. If it fails because ffmpeg
    is missing, use send_file instead.

    Args:
        recipient: Who gets it, as "Name (jid)" with the exact name and JID from list_chats
            or search_contacts, e.g. "Dana Levi (972501234567@s.whatsapp.net)" or
            "Family (120363000000000000@g.us)". For a number with no chat yet use
            "Name (972501234567)". The user reviews this before approving, and a name
            that does not match the chat is refused.
        media_path: Absolute path to the audio file (converted to Opus .ogg if needed)
        mark_read: Also mark the chat read (sending read receipts) after it is sent
    """
    jid, err = _resolve_chat(recipient)
    if err:
        return {"success": False, "message": err}
    success, status_message = whatsapp_audio_voice_message(jid, media_path)
    return _with_mark_read(jid, success, status_message, mark_read)

@mcp.tool(annotations=_READ)
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
    if _nameless(name, jid) and not lookup.endswith("@s.whatsapp.net"):
        return None, _NAMELESS
    chat = whatsapp_get_chat(lookup, include_last_message=False)
    actual = (chat.name if chat else None) or ""
    if actual and name.casefold() != actual.casefold():
        return None, f"Name does not match this chat (stored name: {actual}). Nothing was done; retry with the stored name."
    return jid, None


_NAMELESS = ("The name is just the chat's number, so the user can't tell what they approve. "
             "Use its real name (list_chats, list_channels or search_contacts); nothing was done.")


def _nameless(name: str, jid: str) -> bool:
    """True when the "name" is only the JID or its number, not a readable name."""
    n = (name or "").strip()
    return not n or n == jid or n == jid.split("@")[0] or n.lstrip("+").isdigit() and "@" in jid and not jid.endswith("@s.whatsapp.net")


def _needs_summary(entry: str) -> Optional[str]:
    """Error message when a "Name (jid)" entry lacks the "— summary" the user approves."""
    m = _CHAT_ENTRY.match(entry or "")
    if m and not m["summary"]:
        return ('Add a short summary after the JID so the user sees what they approve, '
                'e.g. "Family (1203…@g.us) — 12 unread, last: \'Who brings dessert?…\'"')
    return None


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


@mcp.tool(annotations=_WRITE)
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


@mcp.tool(annotations=_WRITE)
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


@mcp.tool(annotations=_WRITE)
@needs_bridge
def mark_chat_unread(chat: str) -> Dict[str, Any]:
    """Mark a WhatsApp chat as unread on all your devices (a reminder to come back to it).
    It does not undo read receipts already sent.

    Args:
        chat: The chat as "Name (jid) — summary" with the exact name and JID from list_chats
            and why it is being flagged, e.g.
            "Dana Levi (972501234567@s.whatsapp.net) — asked about Friday dinner, needs an answer"
    """
    err = _needs_summary(chat)
    if err:
        return {"success": False, "message": err}
    jid, err = _resolve_chat(chat)
    if err:
        return {"success": False, "message": err}
    success, message = whatsapp_mark_chat(jid, read=False)
    return {"success": success, "message": message}




def _leave_groups(groups: List[str], delete_chat: bool) -> Dict[str, Any]:
    results = []
    for entry in dict.fromkeys(groups):  # dedupe, keep order
        m = _CHAT_ENTRY.match(entry or "")
        if m and not m["summary"]:
            results.append({"chat": entry, "success": False, "message":
                            'Add a summary after the JID, e.g. "Building 5 Parents (1203…@g.us) — '
                            'last active 8 months ago, you never wrote"'})
            continue
        jid, err = _resolve_chat(entry)
        if err:
            results.append({"chat": entry, "success": False, "message": err})
            continue
        if not _is_group(jid):
            results.append({"chat": entry, "success": False, "message": "Not a group or community; for a channel use unfollow_channels"})
            continue
        ok, msg = whatsapp_leave_group(jid, delete_chat)
        results.append({"chat": entry, "success": ok, "message": msg})
    done = sum(r["success"] for r in results)
    return {"success": done == len(results), "done": done, "failed": len(results) - done, "results": results}


@mcp.tool(annotations=_DESTRUCTIVE)
@needs_bridge
def leave_groups(groups: List[str]) -> Dict[str, Any]:
    """DESTRUCTIVE: exit one or more WhatsApp groups or communities. Leaving a community
    (its JID or its announcement group's JID) also leaves every group you are in inside it.
    Members see that you left, and
    you can only rejoin with an invite link or if an admin re-adds you. The chat and
    its history stay in your list; use leave_and_delete_groups to remove them too.

    Args:
        groups: One entry per group as "Group name (jid) — summary", using the exact
            name and JID from list_chats. The summary is what the user approves, so make it
            concrete: when it was last active, whether the user ever wrote there, and a
            truncated quote of the latest message, e.g.
            ["Building 5 Parents (120363000000000000@g.us) — last active 8 months ago, you never wrote: 'Lost keys…'"].
            A name that does not match the chat is refused. Only include groups the user
            chose to leave; never decide on their behalf.
    """
    return _leave_groups(groups, delete_chat=False)


@mcp.tool(annotations=_DESTRUCTIVE)
@needs_bridge
def leave_and_delete_groups(groups: List[str]) -> Dict[str, Any]:
    """DESTRUCTIVE AND IRREVERSIBLE: exit one or more WhatsApp groups or communities
    (a community takes its groups with it) and delete each
    chat with its messages and media on all your devices. Members see that you left.
    Deleted history cannot be recovered.

    Args:
        groups: One entry per group as "Group name (jid) — summary", using the exact
            name and JID from list_chats. The summary is what the user approves, so make it
            concrete: when it was last active, whether the user ever wrote there, and a
            truncated quote of the latest message, e.g.
            ["Building 5 Parents (120363000000000000@g.us) — last active 8 months ago, you never wrote: 'Lost keys…'"].
            A name that does not match the chat is refused. Only include groups the user
            chose to leave; never decide on their behalf.
    """
    return _leave_groups(groups, delete_chat=True)


@mcp.tool(annotations=_READ)
@needs_bridge
def list_channels() -> Dict[str, Any]:
    """List the WhatsApp channels (newsletters) you follow, with name, JID
    (…@newsletter), subscriber count and your role. Channels don't appear in list_chats."""
    return whatsapp_list_channels()


@mcp.tool(annotations=_DESTRUCTIVE)
@needs_bridge
def unfollow_channels(channels: List[str]) -> Dict[str, Any]:
    """DESTRUCTIVE: unfollow one or more WhatsApp channels. You can follow again later.

    Args:
        channels: One entry per channel as "Channel name (jid@newsletter) — summary", using
            the exact name and JID from list_channels, e.g.
            ["Secret Flights (120363000000000000@newsletter) — flight deals, 40k followers, you never read it"].
            The user sees these before approving: a bare number as the name, a name that
            does not match, or a missing summary is refused. Only include channels the user
            chose to unfollow.
    """
    known = {c["jid"]: c["name"] for c in whatsapp_list_channels().get("channels", [])}
    results = []
    for entry in dict.fromkeys(channels):
        name, jid = _parse_chat_entry(entry or "")
        if _needs_summary(entry):
            results.append({"channel": entry, "success": False, "message": _needs_summary(entry)})
        elif jid and _nameless(name, jid):
            results.append({"channel": entry, "success": False, "message": _NAMELESS})
        elif not jid or not jid.endswith("@newsletter"):
            results.append({"channel": entry, "success": False, "message": 'Use "Channel name (jid@newsletter)" from list_channels'})
        elif jid not in known:
            results.append({"channel": entry, "success": False, "message": "You don't follow this channel"})
        elif name.casefold() != known[jid].casefold():
            results.append({"channel": entry, "success": False, "message": f"Name does not match (channel name: {known[jid]})"})
        else:
            ok, msg = whatsapp_unfollow_channel(jid)
            results.append({"channel": entry, "success": ok, "message": msg})
    done = sum(r["success"] for r in results)
    return {"success": done == len(results), "done": done, "failed": len(results) - done, "results": results}


@mcp.tool(annotations=_WRITE)
@needs_bridge
def fetch_chat_history(chat: str, count: int = 50) -> Dict[str, Any]:
    """Pull older messages for a chat from the user's phone, on demand.

    Use this when a chat has little or no stored text (history never synced, or you
    need more context than list_messages returns) before concluding there is nothing
    in it. It asks the phone for up to `count` messages older than the oldest stored
    one, waits up to ~20 seconds for them, and stores them. Then call list_messages
    again to read them. Call it repeatedly to page further back. The phone must be
    online; if it returns 0, say the history could not be fetched rather than guessing.

    Args:
        chat: The chat as "Name (jid)" with the exact name and JID from list_chats,
            e.g. "Family (120363000000000000@g.us)".
        count: How many older messages to request (1-500, default 50).
    """
    jid, err = _resolve_chat(chat)
    if err:
        return {"success": False, "message": err}
    return whatsapp_fetch_history(jid, count)


@mcp.tool(annotations=_WRITE)
@needs_bridge
def sync_unread(max_chats: int = 40) -> Dict[str, Any]:
    """Reconcile unread chats with the user's phone after the bridge was down.

    Reads the phone's read / marked-unread markers, clears stale unread entries
    (deleted chats, unfollowed channels, groups left), and pulls the newest messages
    only for threads that look unread (never ones the phone shows as read). Sends no
    read receipts. Takes up to a few minutes. Call it when list_chats or bridge_status
    reports drift=true, or when the phone shows unread chats that list_chats lacks.

    Args:
        max_chats: Most threads to backfill in one run (1-50, default 40).
    """
    return whatsapp_sync_unread(max_chats)


@mcp.tool(annotations=_READ)
@needs_bridge
def list_chat_lists(refresh: bool = False) -> Dict[str, Any]:
    """List the user's WhatsApp chat lists (the filters in the chat list, e.g. Favorites
    and custom lists) with the chats in each.

    Use this before organizing chats into lists, to reuse existing lists instead of
    creating duplicates. Pass refresh=True to re-sync from WhatsApp if lists were
    changed on the phone and look stale.
    """
    return whatsapp_get_lists(refresh)


@mcp.tool(annotations=_WRITE)
@needs_bridge
def create_chat_list(name: str) -> Dict[str, Any]:
    """Create a new custom WhatsApp chat list. Returns its list_id.

    Check list_chat_lists first so you don't create a duplicate. Confirm the name
    with the user before creating it.
    """
    return whatsapp_create_list(name)


@mcp.tool(annotations=_WRITE)
@needs_bridge
def update_chat_list(chat_list: str, new_name: str = "", delete: bool = False) -> Dict[str, Any]:
    """Rename a custom chat list (pass new_name) or delete it (delete=True).

    Deleting removes only the list, not its chats. Built-in lists can't be changed.
    Confirm with the user before deleting.

    Args:
        chat_list: The list as "List name (list_id)" from list_chat_lists, e.g. "Work (3)".
        new_name: The new name, when renaming.
        delete: Delete the list instead of renaming it.
    """
    list_id, err = _resolve_list(chat_list)
    if err:
        return {"success": False, "message": err}
    return whatsapp_edit_list(list_id, new_name, delete)


def _resolve_list(entry: str):
    """Turn "List name (list_id)" into a list_id, checking the name matches."""
    m = re.match(r"^\s*(.*?)\s*\(([^()]+)\)\s*$", entry or "")
    if not m:
        return None, 'Use "List name (list_id)" from list_chat_lists, e.g. "Work (3)"'
    lists = {str(l.get("id")): l.get("name", "") for l in whatsapp_get_lists(False).get("lists") or []}
    if m[2] not in lists:
        return None, "No list with that id; check list_chat_lists"
    if m[1].casefold() != lists[m[2]].casefold():
        return None, f"Name does not match this list (its name: {lists[m[2]]}). Nothing was done."
    return m[2], None


@mcp.tool(annotations=_WRITE)
@needs_bridge
def set_chats_in_list(chat_list: str, chats: List[str], add: bool = True) -> Dict[str, Any]:
    """Add chats to a list (add=True) or remove them (add=False), in one batch.

    Use it for ongoing organization: propose a grouping, get the user's OK, then
    apply it. The change syncs to the phone.

    Args:
        chat_list: The list as "List name (list_id)" from list_chat_lists, e.g. "Work (3)".
        chats: One entry per chat as "Chat name (jid)" with the exact name and JID from
            list_chats, e.g. ["Family (120363000000000000@g.us)"]. The user reviews these
            names before approving; a name that does not match the chat is refused.
        add: True to add the chats, False to remove them.
    """
    list_id, err = _resolve_list(chat_list)
    if err:
        return {"success": False, "message": err}
    jids, errors = [], []
    for entry in dict.fromkeys(chats):
        jid, e = _resolve_chat(entry)
        if e:
            errors.append({"chat": entry, "message": e})
        else:
            jids.append(jid)
    if errors:
        return {"success": False, "message": "Nothing was changed; fix these entries", "errors": errors}
    return whatsapp_set_list_chats(list_id, jids, add)


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