import sqlite3
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os.path
import requests
import json
import re
import unicodedata
import audio

MESSAGES_DB_PATH = os.path.join(
    os.environ.get('WHATSAPP_STORE_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store')),
    'messages.db',
)
from bridge import API_BASE_URL as WHATSAPP_API_BASE_URL

_IGNORABLE = re.compile(r"[\u0591-\u05C7\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF]")


def normalize_name(text: Optional[str]) -> str:
    """Fold a name for matching: strips Hebrew niqqud/cantillation and bidi
    marks, unifies quote-like characters, and casefolds Latin text."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _IGNORABLE.sub("", text)
    text = text.replace("\u05F3", "'").replace("\u05F4", '"').replace("\u2019", "'")
    return text.casefold().strip()


def connect() -> sqlite3.Connection:
    """Open messages.db with a busy timeout and the `norm()` SQL function."""
    conn = sqlite3.connect(MESSAGES_DB_PATH, timeout=5)
    conn.create_function("norm", 1, normalize_name, deterministic=True)
    return conn


def attach_read_state(conn: sqlite3.Connection, chats: list) -> None:
    """Fill unread_count / marked_unread on Chat objects from the chats table."""
    for chat in chats:
        if chat is None:
            continue
        row = conn.execute(
            "SELECT unread_count, marked_unread FROM chats WHERE jid = ?", (chat.jid,)
        ).fetchone()
        if row:
            chat.unread_count, chat.marked_unread = row[0] or 0, bool(row[1])


def user_part(jid: Optional[str]) -> str:
    return (jid or "").split("@")[0].split(":")[0]


@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None
    last_sender_name: Optional[str] = None
    unread_count: int = 0
    marked_unread: bool = False

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str
    lid: Optional[str] = None
    push_name: Optional[str] = None
    name_source: Optional[str] = None
    is_group: bool = False

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]

def get_sender_name(sender_jid: str) -> str:
    """Display name for a sender stored as a bare user part or a full JID."""
    try:
        conn = connect()
        row = conn.execute(
            "SELECT name FROM contacts WHERE user = ? AND name_source != 'fallback'",
            (user_part(sender_jid),),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT name FROM chats WHERE jid = ? OR jid LIKE ? LIMIT 1",
                (sender_jid, f"{user_part(sender_jid)}@%"),
            ).fetchone()
        return row[0] if row and row[0] else sender_jid
    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if 'conn' in locals():
            conn.close()

def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""
    
    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
        
    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
    
    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"
        if not message.is_from_me and sender_name != message.sender:
            sender_name = f"{sender_name} ({message.sender})"
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output
    
    for message in messages:
        output += format_message(message, show_chat_info)
    return output

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
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    try:
        conn = connect()
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []
        
        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
            
            where_clauses.append("julianday(messages.timestamp) > julianday(?)")
            params.append(after.isoformat())

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")
            
            where_clauses.append("julianday(messages.timestamp) < julianday(?)")
            params.append(before.isoformat())

        if sender_phone_number:
            from search import sender_clause
            where_clauses.append(sender_clause(conn, sender_phone_number, params).replace("m.sender", "messages.sender"))
            
        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)
            
        if query:
            for word in query.split():
                where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
                params.append(f"%{word}%")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY julianday(messages.timestamp) DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()
        
        result = []
        for msg in messages:
            message = Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            )
            result.append(message)
            
        if include_context is None:
            include_context = bool(query)
        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after, msg.chat_jid)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)
            
            return format_messages_list(messages_with_context, show_chat_info=True)
            
        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True)    
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5,
    chat_jid: Optional[str] = None,
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = connect()
        cursor = conn.cursor()
        
        # Get the target message first
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ? AND (? IS NULL OR messages.chat_jid = ?)
            ORDER BY julianday(messages.timestamp) DESC LIMIT 1
        """, (message_id, chat_jid, chat_jid))
        msg_data = cursor.fetchone()
        
        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found" + (f" in {chat_jid}" if chat_jid else ""))
            
        target_message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8]
        )
        
        # Get messages before
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND julianday(messages.timestamp) < julianday(?)
            ORDER BY julianday(messages.timestamp) DESC
            LIMIT ?
        """, (msg_data[7], msg_data[0], before))
        
        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        # Get messages after
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND julianday(messages.timestamp) > julianday(?)
            ORDER BY julianday(messages.timestamp) ASC
            LIMIT ?
        """, (msg_data[7], msg_data[0], after))
        
        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
    unread_only: bool = False
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    try:
        conn = connect()
        cursor = conn.cursor()
        
        # Build base query
        last_cols = (
            "messages.content, messages.sender, messages.is_from_me"
            if include_last_message
            else "NULL, NULL, NULL"
        )
        query_parts = [f"""
            SELECT
                chats.jid,
                chats.name,
                chats.last_message_time,
                {last_cols}
            FROM chats
        """]
        
        if include_last_message:
            query_parts.append("""
                LEFT JOIN messages ON messages.rowid = (
                    SELECT rowid FROM messages lm WHERE lm.chat_jid = chats.jid
                    ORDER BY julianday(lm.timestamp) DESC LIMIT 1)
            """)
            
        where_clauses = []
        params = []
        
        if query:
            where_clauses.append("(norm(chats.name) LIKE ? OR chats.jid LIKE ?)")
            params.extend([f"%{normalize_name(query)}%", f"%{query}%"])

        if unread_only:
            where_clauses.append("(chats.unread_count > 0 OR chats.marked_unread)")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add sorting
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")
        
        # Add pagination
        offset = (page ) * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            if chat.last_sender and not chat.last_is_from_me:
                chat.last_sender_name = get_sender_name(chat.last_sender)
            result.append(chat)

        attach_read_state(conn, result)
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search people by name (Hebrew or English, saved or push name) or number.

    One result per person: phone and LID identities are merged. Exact and
    prefix matches come first, then people you have chatted with recently.
    """
    q = normalize_name(query)
    digits = re.sub(r"\D", "", query)
    if not q:
        return []
    try:
        conn = connect()
        rows = conn.execute(
            """
            SELECT k.user, k.jid, k.phone, k.lid, k.name, k.name_source, k.push_name,
                   (SELECT MAX(c.last_message_time) FROM chats c
                     WHERE c.jid IN (k.phone || '@s.whatsapp.net', k.lid || '@lid')) AS last_chat
            FROM contacts k
            WHERE norm(k.name) LIKE :p OR norm(k.full_name) LIKE :p OR norm(k.first_name) LIKE :p
               OR norm(k.push_name) LIKE :p OR norm(k.business_name) LIKE :p
               OR (:d != '' AND (k.phone LIKE :dp OR k.user LIKE :dp))
            """,
            {"p": f"%{q}%", "d": digits, "dp": f"%{digits}%"},
        ).fetchall()

        people = {}
        for user, jid, phone, lid, name, source, push, last_chat in rows:
            key = phone or lid or user
            rank = (
                0 if normalize_name(name) == q else 1 if normalize_name(name).startswith(q) else 2,
                source == "fallback",
                -(datetime.fromisoformat(last_chat).timestamp() if last_chat else 0),
            )
            if key not in people or rank < people[key][0]:
                people[key] = (rank, Contact(
                    phone_number=phone or "",
                    name=name,
                    jid=f"{phone}@s.whatsapp.net" if phone else jid,
                    lid=f"{lid}@lid" if lid else None,
                    push_name=push,
                    name_source=source,
                ))
        found = [c for _, c in sorted(people.values(), key=lambda x: x[0])[:50]]
        groups = conn.execute(
            """SELECT jid, name FROM chats WHERE jid LIKE '%@g.us' AND norm(name) LIKE ?
               ORDER BY julianday(last_message_time) DESC LIMIT 20""",
            (f"%{q}%",),
        ).fetchall()
        found += [Contact(phone_number="", name=n, jid=j, name_source="group", is_group=True) for j, n in groups]
        return found

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn = connect()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, (jid, jid, limit, page * limit))
        
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = connect()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY m.timestamp DESC
            LIMIT 1
        """, (jid, jid))
        
        msg_data = cursor.fetchone()
        
        if not msg_data:
            return None
            
        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )
        
        return format_message(message)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = connect()
        cursor = conn.cursor()
        
        query = """
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
        """
        
        if include_last_message:
            query += """
                LEFT JOIN messages m ON m.rowid = (
                SELECT rowid FROM messages lm WHERE lm.chat_jid = c.jid
                ORDER BY julianday(lm.timestamp) DESC LIMIT 1)
            """
            
        query += " WHERE c.jid = ?"
        
        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        chat = Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        attach_read_state(conn, [chat])
        return chat
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    try:
        conn = connect()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON m.rowid = (
                SELECT rowid FROM messages lm WHERE lm.chat_jid = c.jid
                ORDER BY julianday(lm.timestamp) DESC LIMIT 1)
            WHERE c.jid LIKE ? AND c.jid NOT LIKE '%@g.us'
            LIMIT 1
        """, (f"%{sender_phone_number}%",))
        
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        chat = Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        attach_read_state(conn, [chat])
        return chat
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = requests.post(url, json=payload)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None
            
    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None


def mark_chat(chat_jid: str, read: bool = True) -> Tuple[bool, str]:
    """Mark a chat read (sending read receipts) or unread via the bridge."""
    try:
        resp = requests.post(f"{WHATSAPP_API_BASE_URL}/mark-read",
                             json={"chat_jid": chat_jid, "read": read}, timeout=30)
        try:
            data = resp.json()
        except ValueError:
            return False, f"Bridge returned HTTP {resp.status_code}: {resp.text[:200]}"
        return bool(data.get("success")), data.get("message", "")
    except requests.RequestException as e:
        return False, f"Request error: {e}"


def leave_group(chat_jid: str, delete_chat: bool = False) -> Tuple[bool, str]:
    """Leave a group via the bridge, optionally deleting the chat on all devices."""
    try:
        resp = requests.post(f"{WHATSAPP_API_BASE_URL}/leave-group",
                             json={"chat_jid": chat_jid, "delete_chat": delete_chat}, timeout=30)
        try:
            data = resp.json()
        except ValueError:
            return False, f"Bridge returned HTTP {resp.status_code}: {resp.text[:200]}"
        return bool(data.get("success")), data.get("message", "")
    except requests.RequestException as e:
        return False, f"Request error: {e}"
