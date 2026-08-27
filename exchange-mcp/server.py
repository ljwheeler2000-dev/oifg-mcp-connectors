#!/usr/bin/env python3
"""
MCP Server for Microsoft Exchange (EWS).

Connects to a Smarsh-hosted Exchange server via Exchange Web Services (EWS)
and exposes calendar and email tools for use with Claude/Cowork.
"""

import json
import os
import asyncio
from datetime import datetime
from typing import Optional, List
from functools import partial
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Core exchangelib imports — these were stable in the version used to build this server
from exchangelib import (
    Credentials, Account, Configuration, DELEGATE,
    CalendarItem, Message, Mailbox, HTMLBody, EWSDateTime,
)

# Contact-related classes may live in different submodules across versions
import importlib as _importlib

def _safe_import(module, attr):
    try:
        return getattr(_importlib.import_module(module), attr)
    except (ImportError, AttributeError):
        return None

Contact        = (_safe_import("exchangelib", "Contact")
                  or _safe_import("exchangelib.items.contact", "Contact"))

def _contact_field_value_cls(field_name):
    """Get the exact value_cls the Contact class expects for a given field name."""
    try:
        if Contact is None:
            return None
        for field in Contact.FIELDS:
            if field.name == field_name:
                return getattr(field, 'value_cls', None)
    except Exception:
        pass
    return None

PhoneNumber    = (_safe_import("exchangelib.indexed_properties", "PhoneNumber")
                  or _contact_field_value_cls("phone_numbers")
                  or _safe_import("exchangelib", "PhoneNumber")
                  or _safe_import("exchangelib.indexed_fields", "PhoneNumber")
                  or _safe_import("exchangelib.items.contact", "PhoneNumber")
                  or _safe_import("exchangelib.fields", "PhoneNumber")
                  or _safe_import("exchangelib.properties", "PhoneNumber"))
EWSEmailAddress = (_safe_import("exchangelib", "EmailAddress")
                   or _safe_import("exchangelib.indexed_fields", "EmailAddress")
                   or _safe_import("exchangelib.items.contact", "EmailAddress")
                   or _safe_import("exchangelib.properties", "EmailAddress"))
# Contact email addresses require the indexed_properties variant (different from EWSEmailAddress)
ContactEmailAddress = (_safe_import("exchangelib.indexed_properties", "EmailAddress")
                       or _safe_import("exchangelib", "EmailAddress"))
Attendee       = (_safe_import("exchangelib", "Attendee")
                  or _safe_import("exchangelib.properties", "Attendee"))
Folder         = (_safe_import("exchangelib", "Folder")
                  or _safe_import("exchangelib.folders", "Folder"))

# Flag class for flagging emails — lives in extended_properties in exchangelib 5.x
_EWSFlag = (_safe_import("exchangelib.extended_properties", "Flag")
            or _safe_import("exchangelib", "Flag")
            or _safe_import("exchangelib.items", "Flag")
            or _safe_import("exchangelib.items.message", "Flag")
            or _safe_import("exchangelib.properties", "Flag"))

# Register Flag as an extended property on Message so item.flag = value works
# PR_FLAG_STATUS: 0=NotFlagged, 1=Flagged, 2=Complete
_FLAG_STATUS_MAP = {"NotFlagged": 0, "Flagged": 1, "Complete": 2}
if _EWSFlag is not None:
    try:
        Message.register("flag", _EWSFlag)
    except Exception:
        pass  # already registered or not supported

from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

load_dotenv()

# Write a startup diagnostic so we can see what imported successfully
_LOG_PATH = os.path.join(os.path.dirname(__file__), "startup.log")
with open(_LOG_PATH, "w") as _lf:
    _lf.write(f"Contact={Contact}\n")
    _lf.write(f"PhoneNumber={PhoneNumber}\n")
    _lf.write(f"PhoneNumber.FIELDS={getattr(PhoneNumber, 'FIELDS', 'N/A')}\n")
    _lf.write(f"EWSEmailAddress={EWSEmailAddress}\n")
    _lf.write(f"ContactEmailAddress={ContactEmailAddress}\n")
    if ContactEmailAddress:
        _lf.write(f"ContactEmailAddress.FIELDS={getattr(ContactEmailAddress, 'FIELDS', 'N/A')}\n")
    _lf.write(f"Attendee={Attendee}\n")
    _lf.write(f"Folder={Folder}\n")
    _lf.write(f"_EWSFlag={_EWSFlag}\n")
    _lf.write(f"_EWSFlag registered on Message={hasattr(Message, 'flag')}\n")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
EWS_SERVER   = os.getenv("EWS_SERVER", "mail.smarshmail.com")
EWS_EMAIL    = os.getenv("EWS_EMAIL", "")
EWS_USERNAME = os.getenv("EWS_USERNAME", "")   # domain\username or email
EWS_PASSWORD = os.getenv("EWS_PASSWORD", "")
EWS_TIMEZONE = os.getenv("EWS_TIMEZONE", "America/Indiana/Indianapolis")

# ---------------------------------------------------------------------------
# Module-level Exchange account (initialized once at startup)
# ---------------------------------------------------------------------------
_credentials = Credentials(username=EWS_USERNAME, password=EWS_PASSWORD)
_config = Configuration(server=EWS_SERVER, credentials=_credentials)
_account = Account(
    primary_smtp_address=EWS_EMAIL,
    config=_config,
    autodiscover=False,
    access_type=DELEGATE,
)

# DNS-rebinding protection is enabled by default and checks the incoming
# Host header against an allowlist. That allowlist can't be hardcoded to a
# domain ahead of time (Railway's domain isn't known at build time, and
# every advisor who deploys this template gets a different one), so it's
# disabled here. Our own bearer-token auth (added below, in the entry
# point) is the real access control for the remote deployment; stdio/local
# runs are unaffected since this setting only matters for HTTP transport.
mcp = FastMCP(
    "exchange_mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_account() -> Account:
    return _account


def _ews_dt(iso_str: str, tz_name: str = EWS_TIMEZONE) -> EWSDateTime:
    """Parse an ISO 8601 string to an EWSDateTime in the given timezone."""
    tz = ZoneInfo(tz_name)
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    return EWSDateTime.from_datetime(dt)


def _fmt_event(item: CalendarItem) -> dict:
    return {
        "id": item.id,
        "subject": item.subject,
        "start": str(item.start),
        "end": str(item.end),
        "location": item.location or "",
        "organizer": str(item.organizer.email_address) if item.organizer else "",
        "attendees": [
            a.mailbox.email_address
            for a in (item.required_attendees or [])
        ],
        "body": (item.text_body or "")[:500],
        "is_all_day": item.is_all_day,
    }


def _fmt_email(item: Message) -> dict:
    return {
        "id": item.id,
        "subject": item.subject,
        "from": str(item.sender.email_address) if item.sender else "",
        "to": [r.email_address for r in (item.to_recipients or [])],
        "received": str(item.datetime_received),
        "is_read": item.is_read,
        "body": (item.text_body or "")[:1000],
    }


def _fmt_contact(item) -> dict:
    phones = {}
    for p in (item.phone_numbers or []):
        key = getattr(p, "type", None) or getattr(p, "label", str(p))
        val = getattr(p, "number", None) or getattr(p, "phone_number", str(p))
        phones[str(key)] = str(val)
    emails = {}
    for e in (item.email_addresses or []):
        key = getattr(e, "label", None) or getattr(e, "type", str(e))
        val = getattr(e, "email_address", None) or getattr(e, "email", None) or getattr(e, "address", str(e))
        emails[str(key)] = str(val)
    return {
        "id": item.id,
        "display_name": item.display_name or "",
        "given_name": item.given_name or "",
        "surname": item.surname or "",
        "company_name": item.company_name or "",
        "job_title": item.job_title or "",
        "emails": emails,
        "phones": phones,
    }


async def _run_sync(fn, *args, **kwargs):
    """Run a blocking exchangelib call in a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


def _resolve_folder(account, folder_name: str):
    """Resolve a folder name to an exchangelib folder object.

    Supports standard shorthand ('inbox', 'sent', 'drafts', 'deleted', 'trash')
    plus any custom folder display name (e.g. 'Advisor Evolution', 'Clients').
    Raises ValueError if the folder cannot be found anywhere in the mailbox.
    """
    name = (folder_name or "inbox").strip()
    folder_map = {
        "inbox":   account.inbox,
        "sent":    account.sent,
        "drafts":  account.drafts,
        "deleted": account.trash,
        "trash":   account.trash,
    }
    folder = folder_map.get(name.lower())
    if folder is not None:
        return folder
    # Fall back to a recursive glob across the entire mailbox
    matches = list(account.root.glob(f"**/{name}"))
    if not matches:
        raise ValueError(f"Folder '{name}' not found in mailbox")
    return matches[0]


# ---------------------------------------------------------------------------
# Input Models
# ---------------------------------------------------------------------------

class ListEventsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    start: str = Field(..., description="Start datetime ISO 8601, e.g. '2026-04-27T00:00:00'")
    end: str   = Field(..., description="End datetime ISO 8601, e.g. '2026-04-28T00:00:00'")


class GetEventInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    event_id: str = Field(..., description="Exchange item ID of the calendar event")


class CreateEventInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    subject:   str            = Field(..., description="Event title")
    start:     str            = Field(..., description="Start datetime ISO 8601")
    end:       str            = Field(..., description="End datetime ISO 8601")
    location:  Optional[str]  = Field(None, description="Location or meeting room")
    body:      Optional[str]  = Field(None, description="Event body / description")
    attendees: Optional[List[str]] = Field(default_factory=list, description="List of attendee email addresses")


class UpdateEventInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    event_id:  str            = Field(..., description="Exchange item ID of the event to update")
    subject:   Optional[str]  = Field(None, description="New title")
    start:     Optional[str]  = Field(None, description="New start datetime ISO 8601")
    end:       Optional[str]  = Field(None, description="New end datetime ISO 8601")
    location:  Optional[str]  = Field(None, description="New location")
    body:      Optional[str]  = Field(None, description="New body text")


class DeleteEventInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    event_id: str = Field(..., description="Exchange item ID of the event to delete")


class ListEmailsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    limit:  Optional[int] = Field(default=20, ge=1, le=100, description="Max results (default 20)")
    folder: Optional[str] = Field(default="inbox", description="Folder name — 'inbox', 'sent', 'drafts', or any custom folder (e.g. 'Advisor Evolution')")
    unread_only: Optional[bool] = Field(default=False, description="If True, return only unread emails")


class SearchEmailsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    query:  str           = Field(..., description="Subject keyword to search for")
    limit:  Optional[int] = Field(default=20, ge=1, le=100, description="Max results (default 20)")
    folder: Optional[str] = Field(default="inbox", description="Folder to search — 'inbox', 'sent', 'drafts', or any custom folder")


class SearchEmailsBySenderInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    sender_email: str     = Field(..., description="Sender email address to filter by")
    limit:  Optional[int] = Field(default=20, ge=1, le=100, description="Max results (default 20)")
    folder: Optional[str] = Field(default="inbox", description="Folder to search — 'inbox', 'sent', 'drafts', or any custom folder")


class GetEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email_id: str = Field(..., description="Exchange item ID of the email")
    folder: Optional[str] = Field(default="inbox", description="Folder the email lives in — 'inbox', 'sent', 'drafts', or any custom folder")


class SendEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    to:      List[str]    = Field(..., description="List of recipient email addresses")
    subject: str          = Field(..., description="Email subject")
    body:    str          = Field(..., description="Email body (plain text or HTML)")
    cc:      Optional[List[str]] = Field(default_factory=list, description="CC recipients")


class ReplyToEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email_id: str          = Field(..., description="Exchange item ID of the email to reply to")
    body:     str          = Field(..., description="Reply body text (plain text or HTML)")
    reply_all: Optional[bool] = Field(default=False, description="If True, reply-all; otherwise reply to sender only")
    folder: Optional[str] = Field(default="inbox", description="Folder the original email lives in — 'inbox', 'sent', 'drafts', or any custom folder")


class ForwardEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email_id: str       = Field(..., description="Exchange item ID of the email to forward")
    to:       List[str] = Field(..., description="List of recipient email addresses")
    body:     Optional[str] = Field(None, description="Optional note to prepend to the forwarded message")
    folder: Optional[str] = Field(default="inbox", description="Folder the original email lives in — 'inbox', 'sent', 'drafts', or any custom folder")


class MarkEmailReadInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email_id:  str  = Field(..., description="Exchange item ID of the email")
    is_read:   bool = Field(..., description="True to mark as read, False to mark as unread")
    folder: Optional[str] = Field(default="inbox", description="Folder the email lives in — 'inbox', 'sent', 'drafts', or any custom folder")


class MoveEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email_id:     str = Field(..., description="Exchange item ID of the email to move")
    source_folder: Optional[str] = Field(default="inbox", description="Source folder: 'inbox', 'sent', 'drafts'")
    dest_folder:  str = Field(..., description="Destination folder name (e.g. 'inbox', 'sent', 'deleted', or a custom folder display name)")


class DeleteEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email_id: str = Field(..., description="Exchange item ID of the email to delete")
    folder: Optional[str] = Field(default="inbox", description="Folder the email lives in — 'inbox', 'sent', 'drafts', or any custom folder")


class CreateDraftInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    to:      List[str]         = Field(..., description="List of recipient email addresses")
    subject: str               = Field(..., description="Email subject")
    body:    str               = Field(..., description="Email body (plain text or HTML)")
    cc:      Optional[List[str]] = Field(default_factory=list, description="CC recipients")


class SendDraftInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    draft_id: str = Field(..., description="Exchange item ID of the draft to send (from exchange_create_draft or exchange_list_emails with folder='drafts')")


class FlagEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email_id: str = Field(..., description="Exchange item ID of the email")
    flag_status: str = Field(..., description="Flag status: 'Flagged', 'NotFlagged', or 'Complete'")
    folder: Optional[str] = Field(default="inbox", description="Folder the email lives in — 'inbox', 'sent', 'drafts', or any custom folder")


class SetImportanceInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email_id: str = Field(..., description="Exchange item ID of the email")
    importance: str = Field(..., description="Importance level: 'High', 'Normal', or 'Low'")
    folder: Optional[str] = Field(default="inbox", description="Folder the email lives in — 'inbox', 'sent', 'drafts', or any custom folder")


class SetCategoriesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email_id: str = Field(..., description="Exchange item ID of the email")
    categories: List[str] = Field(..., description="List of category tag names to set (replaces existing categories)")
    folder: Optional[str] = Field(default="inbox", description="Folder the email lives in — 'inbox', 'sent', 'drafts', or any custom folder")


class RemoveCategoriesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email_id: str = Field(..., description="Exchange item ID of the email")
    categories: Optional[List[str]] = Field(default=None, description="Categories to remove. If omitted, removes ALL categories.")
    folder: Optional[str] = Field(default="inbox", description="Folder the email lives in — 'inbox', 'sent', 'drafts', or any custom folder")


class CreateDraftReplyInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email_id: str = Field(..., description="Exchange item ID of the email to reply to")
    body: str = Field(..., description="Reply body (plain text or HTML)")
    reply_all: Optional[bool] = Field(default=False, description="If True, addresses all original recipients; otherwise sender only")
    folder: Optional[str] = Field(default="inbox", description="Folder the original email lives in — 'inbox', 'sent', 'drafts', or any custom folder")


class AddAttendeesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    event_id: str = Field(..., description="Exchange item ID of the calendar event")
    attendees: List[str] = Field(..., description="List of attendee email addresses to add")


class CopyEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email_id: str = Field(..., description="Exchange item ID of the email to copy")
    source_folder: Optional[str] = Field(default="inbox", description="Source folder: 'inbox', 'sent', 'drafts'")
    dest_folder: str = Field(..., description="Destination folder name ('inbox', 'sent', 'deleted', or custom folder name)")


class CreateFolderInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(..., description="Name of the new folder to create")
    parent_folder: Optional[str] = Field(default="inbox", description="Parent folder: 'inbox', 'sent', 'drafts', or 'root'")


class CreateContactInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    given_name: Optional[str] = Field(None, description="First name")
    surname: Optional[str] = Field(None, description="Last name")
    display_name: Optional[str] = Field(None, description="Display name (defaults to 'First Last' if not set)")
    company_name: Optional[str] = Field(None, description="Company / organization name")
    job_title: Optional[str] = Field(None, description="Job title")
    email: Optional[str] = Field(None, description="Primary email address")
    mobile_phone: Optional[str] = Field(None, description="Mobile phone number")
    business_phone: Optional[str] = Field(None, description="Business phone number")


class UpdateContactInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    contact_id: str = Field(..., description="Exchange item ID of the contact to update")
    given_name: Optional[str] = Field(None, description="New first name")
    surname: Optional[str] = Field(None, description="New last name")
    display_name: Optional[str] = Field(None, description="New display name")
    company_name: Optional[str] = Field(None, description="New company name")
    job_title: Optional[str] = Field(None, description="New job title")
    email: Optional[str] = Field(None, description="New primary email address (label 'EmailAddress1')")
    mobile_phone: Optional[str] = Field(None, description="New mobile phone number")
    business_phone: Optional[str] = Field(None, description="New business phone number")


class DeleteContactInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    contact_id: str = Field(..., description="Exchange item ID of the contact to delete")


class FindContactsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    query: str = Field(..., description="Search string matched against display name, given name, surname, company, or email")
    limit: Optional[int] = Field(default=20, ge=1, le=100, description="Max results (default 20)")


# ---------------------------------------------------------------------------
# Calendar Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="exchange_list_calendar_events",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_list_calendar_events(params: ListEventsInput) -> str:
    """List calendar events within a date/time range.

    Returns all events between start and end datetimes from the Exchange calendar.

    Args:
        params.start: ISO 8601 start datetime (e.g. '2026-04-27T00:00:00')
        params.end:   ISO 8601 end datetime   (e.g. '2026-04-28T00:00:00')

    Returns:
        JSON array of events with id, subject, start, end, location, attendees, body.
    """
    try:
        account = _get_account()
        start_dt = _ews_dt(params.start)
        end_dt   = _ews_dt(params.end)

        def _fetch():
            return list(
                account.calendar.view(start=start_dt, end=end_dt)
                .only("id", "subject", "start", "end", "location",
                      "organizer", "required_attendees", "text_body", "is_all_day")
                .order_by("start")
            )

        items = await _run_sync(_fetch)
        return json.dumps([_fmt_event(i) for i in items], indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_get_calendar_event",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_get_calendar_event(params: GetEventInput) -> str:
    """Get full details of a single calendar event by its Exchange ID.

    Args:
        params.event_id: Exchange item ID (from exchange_list_calendar_events)

    Returns:
        JSON object with full event details.
    """
    try:
        account = _get_account()

        def _fetch():
            return account.calendar.get(id=params.event_id)

        item = await _run_sync(_fetch)
        return json.dumps(_fmt_event(item), indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_create_calendar_event",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def exchange_create_calendar_event(params: CreateEventInput) -> str:
    """Create a new calendar event in Exchange.

    Args:
        params.subject:   Event title
        params.start:     ISO 8601 start datetime
        params.end:       ISO 8601 end datetime
        params.location:  Optional location
        params.body:      Optional description
        params.attendees: Optional list of attendee email addresses

    Returns:
        JSON with the new event's id and subject.
    """
    try:
        account = _get_account()

        def _create():
            item = CalendarItem(
                account=account,
                folder=account.calendar,
                subject=params.subject,
                start=_ews_dt(params.start),
                end=_ews_dt(params.end),
                location=params.location,
                body=HTMLBody(params.body) if params.body else None,
                required_attendees=[
                    Mailbox(email_address=e) for e in (params.attendees or [])
                ] or None,
            )
            item.save(send_meeting_invitations="SendToAllAndSaveCopy")
            return item

        item = await _run_sync(_create)
        return json.dumps({"id": item.id, "subject": item.subject}, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_update_calendar_event",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_update_calendar_event(params: UpdateEventInput) -> str:
    """Update an existing calendar event.

    Args:
        params.event_id: Exchange item ID to update
        params.subject:  New title (optional)
        params.start:    New start datetime ISO 8601 (optional)
        params.end:      New end datetime ISO 8601 (optional)
        params.location: New location (optional)
        params.body:     New body text (optional)

    Returns:
        JSON confirmation with updated fields.
    """
    try:
        account = _get_account()

        def _update():
            item = account.calendar.get(id=params.event_id)
            if params.subject  is not None: item.subject  = params.subject
            if params.start    is not None: item.start    = _ews_dt(params.start)
            if params.end      is not None: item.end      = _ews_dt(params.end)
            if params.location is not None: item.location = params.location
            if params.body     is not None: item.body     = HTMLBody(params.body)
            item.save(send_meeting_invitations="SendToAllAndSaveCopy")
            return item

        item = await _run_sync(_update)
        return json.dumps({"id": item.id, "subject": item.subject, "status": "updated"}, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_delete_calendar_event",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}
)
async def exchange_delete_calendar_event(params: DeleteEventInput) -> str:
    """Delete a calendar event by its Exchange ID.

    Args:
        params.event_id: Exchange item ID to delete

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()

        def _delete():
            item = account.calendar.get(id=params.event_id)
            item.delete(send_meeting_cancellations="SendToAllAndSaveCopy")

        await _run_sync(_delete)
        return json.dumps({"event_id": params.event_id, "status": "deleted"})
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Email Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="exchange_search_emails",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_search_emails(params: SearchEmailsInput) -> str:
    """Search emails by subject keyword in a given folder.

    Args:
        params.query:  Subject keyword to search for
        params.limit:  Max results (default 20, max 100)
        params.folder: 'inbox', 'sent', or 'drafts'

    Returns:
        JSON array of matching emails with id, subject, from, to, received, is_read, body snippet.
    """
    try:
        account = _get_account()
        folder = _resolve_folder(account, params.folder or "inbox")

        def _fetch():
            return list(
                folder.filter(subject__icontains=params.query)
                .only("id", "subject", "sender", "to_recipients",
                      "datetime_received", "is_read", "text_body")
                .order_by("-datetime_received")[: params.limit]
            )

        items = await _run_sync(_fetch)
        return json.dumps([_fmt_email(i) for i in items], indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_get_email",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_get_email(params: GetEmailInput) -> str:
    """Get full content of a single email by its Exchange ID.

    Args:
        params.email_id: Exchange item ID (from exchange_search_emails or exchange_list_emails)
        params.folder:   Folder the email lives in ('inbox', 'sent', 'drafts')

    Returns:
        JSON with full email details including body.
    """
    try:
        account = _get_account()
        folder = _resolve_folder(account, params.folder or "inbox")

        def _fetch():
            return folder.get(id=params.email_id)

        item = await _run_sync(_fetch)
        return json.dumps(_fmt_email(item), indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_send_email",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def exchange_send_email(params: SendEmailInput) -> str:
    """Send an email via Exchange.

    Args:
        params.to:      List of recipient email addresses
        params.subject: Email subject
        params.body:    Email body (plain text or HTML)
        params.cc:      Optional CC list

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()

        def _send():
            msg = Message(
                account=account,
                subject=params.subject,
                body=HTMLBody(params.body),
                to_recipients=[Mailbox(email_address=e) for e in params.to],
                cc_recipients=[Mailbox(email_address=e) for e in (params.cc or [])],
            )
            msg.send_and_save()

        await _run_sync(_send)
        return json.dumps({"status": "sent", "to": params.to, "subject": params.subject})
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Additional Email Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="exchange_list_emails",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_list_emails(params: ListEmailsInput) -> str:
    """List recent emails from a folder without any keyword filter.

    Args:
        params.limit:       Max results (default 20, max 100)
        params.folder:      'inbox', 'sent', or 'drafts'
        params.unread_only: If True, return only unread emails

    Returns:
        JSON array of emails with id, subject, from, to, received, is_read, body snippet.
    """
    try:
        account = _get_account()
        folder = _resolve_folder(account, params.folder or "inbox")

        def _fetch():
            qs = folder.all()
            if params.unread_only:
                qs = qs.filter(is_read=False)
            return list(
                qs.only("id", "subject", "sender", "to_recipients",
                        "datetime_received", "is_read", "text_body")
                .order_by("-datetime_received")[: params.limit]
            )

        items = await _run_sync(_fetch)
        return json.dumps([_fmt_email(i) for i in items], indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_search_emails_by_sender",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_search_emails_by_sender(params: SearchEmailsBySenderInput) -> str:
    """Search emails by sender email address.

    Args:
        params.sender_email: Email address of the sender to filter by
        params.limit:        Max results (default 20, max 100)
        params.folder:       'inbox', 'sent', or 'drafts'

    Returns:
        JSON array of matching emails.
    """
    try:
        account = _get_account()
        folder = _resolve_folder(account, params.folder or "inbox")

        def _fetch():
            # EWS doesn't support filtering by sender address directly;
            # fetch recent emails and filter client-side.
            batch_size = min(params.limit * 10, 200)
            results = []
            for item in (
                folder.all()
                .only("id", "subject", "sender", "to_recipients",
                      "datetime_received", "is_read", "text_body")
                .order_by("-datetime_received")[:batch_size]
            ):
                sender_addr = (
                    str(item.sender.email_address).lower()
                    if item.sender else ""
                )
                if sender_addr == params.sender_email.lower():
                    results.append(item)
                    if len(results) >= params.limit:
                        break
            return results

        items = await _run_sync(_fetch)
        return json.dumps([_fmt_email(i) for i in items], indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_reply_to_email",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def exchange_reply_to_email(params: ReplyToEmailInput) -> str:
    """Reply to an existing email.

    Args:
        params.email_id:  Exchange item ID of the email to reply to
        params.body:      Reply body (plain text or HTML)
        params.reply_all: If True, reply-all; otherwise reply to sender only
        params.folder:    Folder the original email lives in

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()
        folder = _resolve_folder(account, params.folder or "inbox")

        def _reply():
            item = folder.get(id=params.email_id)
            if params.reply_all:
                item.reply_all(subject="Re: " + (item.subject or ""), body=params.body)
            else:
                item.reply(subject="Re: " + (item.subject or ""), body=params.body)

        await _run_sync(_reply)
        return json.dumps({"status": "replied", "email_id": params.email_id, "reply_all": params.reply_all})
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_forward_email",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def exchange_forward_email(params: ForwardEmailInput) -> str:
    """Forward an existing email to new recipients.

    Args:
        params.email_id: Exchange item ID of the email to forward
        params.to:       List of recipient email addresses
        params.body:     Optional note to prepend to the forwarded message
        params.folder:   Folder the original email lives in

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()
        folder = _resolve_folder(account, params.folder or "inbox")

        def _forward():
            item = folder.get(id=params.email_id)
            item.forward(
                subject="Fwd: " + (item.subject or ""),
                body=params.body or "",
                to_recipients=[Mailbox(email_address=e) for e in params.to],
            )

        await _run_sync(_forward)
        return json.dumps({"status": "forwarded", "email_id": params.email_id, "to": params.to})
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_mark_email_read",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_mark_email_read(params: MarkEmailReadInput) -> str:
    """Mark an email as read or unread.

    Args:
        params.email_id: Exchange item ID of the email
        params.is_read:  True to mark as read, False to mark as unread
        params.folder:   Folder the email lives in

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()
        folder = _resolve_folder(account, params.folder or "inbox")

        def _mark():
            item = folder.get(id=params.email_id)
            item.is_read = params.is_read
            item.save(update_fields=["is_read"])

        await _run_sync(_mark)
        return json.dumps({"status": "updated", "email_id": params.email_id, "is_read": params.is_read})
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_move_email",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_move_email(params: MoveEmailInput) -> str:
    """Move an email to a different folder.

    Args:
        params.email_id:      Exchange item ID of the email
        params.source_folder: Source folder ('inbox', 'sent', 'drafts')
        params.dest_folder:   Destination folder ('inbox', 'sent', 'deleted', or custom folder name)

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()
        src  = _resolve_folder(account, params.source_folder or "inbox")
        dest = _resolve_folder(account, params.dest_folder)

        def _move():
            item = src.get(id=params.email_id)
            item.move(to_folder=dest)

        await _run_sync(_move)
        return json.dumps({"status": "moved", "email_id": params.email_id, "dest_folder": params.dest_folder})
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_delete_email",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}
)
async def exchange_delete_email(params: DeleteEmailInput) -> str:
    """Delete (trash) an email.

    Args:
        params.email_id: Exchange item ID of the email to delete
        params.folder:   Folder the email currently lives in

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()
        folder = _resolve_folder(account, params.folder or "inbox")

        def _delete():
            item = folder.get(id=params.email_id)
            item.move_to_trash()

        await _run_sync(_delete)
        return json.dumps({"status": "deleted", "email_id": params.email_id})
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_list_folders",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_list_folders() -> str:
    """List all mail folders in the Exchange mailbox.

    Returns:
        JSON array of folder names and item counts.
    """
    try:
        account = _get_account()

        def _fetch():
            results = []
            for folder in account.root.walk():
                try:
                    results.append({
                        "name": folder.name,
                        "total_count": folder.total_count,
                        "unread_count": folder.unread_count,
                    })
                except Exception:
                    pass
            return results

        folders = await _run_sync(_fetch)
        return json.dumps(folders, indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_send_draft",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def exchange_send_draft(params: SendDraftInput) -> str:
    """Send an existing draft email from the Drafts folder.

    Args:
        params.draft_id: Exchange item ID of the draft (from exchange_create_draft or exchange_list_emails with folder='drafts')

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()

        def _send():
            msg = account.drafts.get(id=params.draft_id)
            msg.send()

        await _run_sync(_send)
        return json.dumps({"status": "sent", "draft_id": params.draft_id})
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_create_draft",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def exchange_create_draft(params: CreateDraftInput) -> str:
    """Create a draft email in the Drafts folder without sending it.

    Args:
        params.to:      List of recipient email addresses
        params.subject: Email subject
        params.body:    Email body (plain text or HTML)
        params.cc:      Optional CC list

    Returns:
        JSON with the draft's id and subject so it can be reviewed or sent later.
    """
    try:
        account = _get_account()

        def _draft():
            msg = Message(
                account=account,
                folder=account.drafts,
                subject=params.subject,
                body=HTMLBody(params.body),
                to_recipients=[Mailbox(email_address=e) for e in params.to],
                cc_recipients=[Mailbox(email_address=e) for e in (params.cc or [])],
            )
            msg.save()
            return msg

        msg = await _run_sync(_draft)
        return json.dumps({"status": "draft_saved", "id": msg.id, "subject": msg.subject}, default=str)
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Extended Email Tools (flag, importance, categories, draft-reply, copy, folder)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="exchange_flag_email",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_flag_email(params: FlagEmailInput) -> str:
    """Flag, unflag, or mark an email as complete.

    Args:
        params.email_id:    Exchange item ID of the email
        params.flag_status: 'Flagged', 'NotFlagged', or 'Complete'
        params.folder:      Folder the email lives in

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()
        folder = _resolve_folder(account, params.folder or "inbox")

        valid = {"Flagged", "NotFlagged", "Complete"}
        if params.flag_status not in valid:
            return f"Error: flag_status must be one of {valid}"

        def _flag():
            if _EWSFlag is None:
                raise RuntimeError("Flag class not available in this version of exchangelib")
            flag_value = _FLAG_STATUS_MAP[params.flag_status]
            item = folder.get(id=params.email_id)
            item.flag = flag_value
            item.save(update_fields=["flag"])

        await _run_sync(_flag)
        return json.dumps({"status": "updated", "email_id": params.email_id, "flag_status": params.flag_status})
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_set_email_importance",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_set_email_importance(params: SetImportanceInput) -> str:
    """Set the importance level (High / Normal / Low) on an email.

    Args:
        params.email_id:   Exchange item ID of the email
        params.importance: 'High', 'Normal', or 'Low'
        params.folder:     Folder the email lives in

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()
        folder = _resolve_folder(account, params.folder or "inbox")

        valid = {"High", "Normal", "Low"}
        if params.importance not in valid:
            return f"Error: importance must be one of {valid}"

        def _set():
            item = folder.get(id=params.email_id)
            item.importance = params.importance
            item.save(update_fields=["importance"])

        await _run_sync(_set)
        return json.dumps({"status": "updated", "email_id": params.email_id, "importance": params.importance})
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_set_email_categories",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_set_email_categories(params: SetCategoriesInput) -> str:
    """Set category tags on an email (replaces any existing categories).

    Args:
        params.email_id:   Exchange item ID of the email
        params.categories: List of category names to apply
        params.folder:     Folder the email lives in

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()
        folder = _resolve_folder(account, params.folder or "inbox")

        def _set():
            item = folder.get(id=params.email_id)
            item.categories = params.categories
            item.save(update_fields=["categories"])

        await _run_sync(_set)
        return json.dumps({"status": "updated", "email_id": params.email_id, "categories": params.categories})
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_remove_email_categories",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_remove_email_categories(params: RemoveCategoriesInput) -> str:
    """Remove category tags from an email.

    Args:
        params.email_id:   Exchange item ID of the email
        params.categories: Specific categories to remove. If omitted, removes ALL categories.
        params.folder:     Folder the email lives in

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()
        folder = _resolve_folder(account, params.folder or "inbox")

        def _remove():
            item = folder.get(id=params.email_id)
            if params.categories is None:
                item.categories = []
            else:
                existing = list(item.categories or [])
                item.categories = [c for c in existing if c not in params.categories]
            item.save(update_fields=["categories"])
            return item.categories

        remaining = await _run_sync(_remove)
        return json.dumps({"status": "updated", "email_id": params.email_id, "remaining_categories": remaining})
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_create_draft_reply",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def exchange_create_draft_reply(params: CreateDraftReplyInput) -> str:
    """Create a draft reply to an email without sending it.

    Args:
        params.email_id:  Exchange item ID of the email to reply to
        params.body:      Reply body (plain text or HTML)
        params.reply_all: If True, addresses all original recipients; otherwise sender only
        params.folder:    Folder the original email lives in

    Returns:
        JSON with the draft reply's id.
    """
    try:
        account = _get_account()
        folder = _resolve_folder(account, params.folder or "inbox")

        def _draft_reply():
            item = folder.get(id=params.email_id)
            # create_reply returns a ReplyToItem; must pass folder explicitly in exchangelib 5.x
            reply = item.create_reply(subject="Re: " + (item.subject or ""), body=params.body)
            reply.save(folder=account.drafts)
            return reply

        msg = await _run_sync(_draft_reply)
        reply_id = getattr(msg, "id", None) or getattr(msg, "item_id", None)
        return json.dumps({"status": "draft_saved", "id": reply_id}, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_add_attendees",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_add_attendees(params: AddAttendeesInput) -> str:
    """Add attendees to an existing calendar event.

    Args:
        params.event_id:  Exchange item ID of the calendar event
        params.attendees: List of email addresses to add as required attendees

    Returns:
        JSON confirmation with updated attendee list.
    """
    try:
        account = _get_account()

        def _add():
            item = account.calendar.get(id=params.event_id)
            existing = [a.mailbox.email_address for a in (item.required_attendees or [])]
            new_emails = [e for e in params.attendees if e not in existing]
            if new_emails:
                current = list(item.required_attendees or [])
                # CalendarItem.required_attendees takes Attendee objects, not Mailbox
                AttendeeClass = Attendee or (lambda mailbox: mailbox)  # fallback
                for e in new_emails:
                    mb = Mailbox(email_address=e)
                    try:
                        current.append(AttendeeClass(mailbox=mb))
                    except TypeError:
                        current.append(mb)  # some versions take Mailbox directly
                item.required_attendees = current
                item.save(send_meeting_invitations="SendToAllAndSaveCopy")
            return item

        item = await _run_sync(_add)
        all_attendees = [a.mailbox.email_address for a in (item.required_attendees or [])]
        return json.dumps({"status": "updated", "event_id": params.event_id, "attendees": all_attendees}, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_copy_email",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def exchange_copy_email(params: CopyEmailInput) -> str:
    """Copy an email to another folder (original stays in place).

    Args:
        params.email_id:      Exchange item ID of the email
        params.source_folder: Source folder ('inbox', 'sent', 'drafts')
        params.dest_folder:   Destination folder name

    Returns:
        JSON confirmation with the new copy's id.
    """
    try:
        account = _get_account()
        src  = _resolve_folder(account, params.source_folder or "inbox")
        dest = _resolve_folder(account, params.dest_folder)

        def _copy():
            item = src.get(id=params.email_id)
            copied = item.copy(to_folder=dest)
            return copied

        copied = await _run_sync(_copy)
        # exchangelib 5.x copy() may return a tuple
        if isinstance(copied, tuple):
            copied = copied[0] if copied else None
        new_id = getattr(copied, "id", None) if copied else None
        return json.dumps({"status": "copied", "new_id": new_id, "dest_folder": params.dest_folder}, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_create_folder",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def exchange_create_folder(params: CreateFolderInput) -> str:
    """Create a new mail folder.

    Args:
        params.name:          Name for the new folder
        params.parent_folder: Parent folder ('inbox', 'sent', 'drafts', or 'root')

    Returns:
        JSON confirmation with the new folder's name.
    """
    try:
        account = _get_account()
        pf = (params.parent_folder or "inbox").strip().lower()
        if pf == "root":
            parent = account.msg_folder_root
        else:
            parent = _resolve_folder(account, params.parent_folder or "inbox")

        def _create():
            if Folder is None:
                raise RuntimeError("Folder class not available in this exchangelib version")
            new_folder = Folder(parent=parent, name=params.name)
            new_folder.save()
            return new_folder

        new_folder = await _run_sync(_create)
        return json.dumps({"status": "created", "name": params.name, "parent": params.parent_folder})
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Contact Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="exchange_find_contacts",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_find_contacts(params: FindContactsInput) -> str:
    """Search contacts by name, company, or email address.

    Args:
        params.query: Search string matched against display name, names, company, or email
        params.limit: Max results (default 20)

    Returns:
        JSON array of matching contacts.
    """
    try:
        account = _get_account()
        q = params.query.lower()

        def _fetch():
            results = []
            for item in account.contacts.all().only(
                "id", "display_name", "given_name", "surname",
                "company_name", "job_title", "phone_numbers", "email_addresses"
            )[:500]:
                haystack = " ".join(filter(None, [
                    item.display_name or "",
                    item.given_name or "",
                    item.surname or "",
                    item.company_name or "",
                    " ".join(e.email for e in (item.email_addresses or []) if e.email),
                ])).lower()
                if q in haystack:
                    results.append(item)
                    if len(results) >= params.limit:
                        break
            return results

        items = await _run_sync(_fetch)
        return json.dumps([_fmt_contact(i) for i in items], indent=2, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_create_contact",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def exchange_create_contact(params: CreateContactInput) -> str:
    """Create a new contact in the Exchange contacts folder.

    Args:
        params.given_name:     First name
        params.surname:        Last name
        params.display_name:   Display name (defaults to 'First Last')
        params.company_name:   Company / organization
        params.job_title:      Job title
        params.email:          Primary email address
        params.mobile_phone:   Mobile phone number
        params.business_phone: Business phone number

    Returns:
        JSON with the new contact's id and display_name.
    """
    try:
        account = _get_account()

        def _create():
            if Contact is None:
                raise RuntimeError("Contact class not available in this exchangelib version")
            display = params.display_name or " ".join(filter(None, [params.given_name, params.surname])) or "Unknown"
            phones = []
            if PhoneNumber and params.mobile_phone:
                try:
                    phones.append(PhoneNumber(label="MobilePhone", phone_number=params.mobile_phone))
                except TypeError:
                    phones.append(PhoneNumber(number=params.mobile_phone, type="MobilePhone"))
            if PhoneNumber and params.business_phone:
                try:
                    phones.append(PhoneNumber(label="BusinessPhone", phone_number=params.business_phone))
                except TypeError:
                    phones.append(PhoneNumber(number=params.business_phone, type="BusinessPhone"))
            emails = []
            if ContactEmailAddress and params.email:
                try:
                    emails.append(ContactEmailAddress(label="EmailAddress1", email=params.email))
                except TypeError:
                    try:
                        emails.append(ContactEmailAddress(name="EmailAddress1", email_address=params.email))
                    except Exception:
                        pass
            contact = Contact(
                account=account,
                folder=account.contacts,
                display_name=display,
                given_name=params.given_name,
                surname=params.surname,
                company_name=params.company_name,
                job_title=params.job_title,
                phone_numbers=phones or None,
                email_addresses=emails or None,
            )
            contact.save()
            return contact

        contact = await _run_sync(_create)
        return json.dumps({"status": "created", "id": contact.id, "display_name": contact.display_name}, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_update_contact",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
)
async def exchange_update_contact(params: UpdateContactInput) -> str:
    """Update an existing contact.

    Args:
        params.contact_id:     Exchange item ID of the contact
        params.given_name:     New first name (optional)
        params.surname:        New last name (optional)
        params.display_name:   New display name (optional)
        params.company_name:   New company name (optional)
        params.job_title:      New job title (optional)
        params.email:          New primary email address (optional)
        params.mobile_phone:   New mobile phone (optional)
        params.business_phone: New business phone (optional)

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()

        def _update():
            contact = account.contacts.get(id=params.contact_id)
            if params.given_name   is not None: contact.given_name   = params.given_name
            if params.surname      is not None: contact.surname      = params.surname
            if params.display_name is not None: contact.display_name = params.display_name
            if params.company_name is not None: contact.company_name = params.company_name
            if params.job_title    is not None: contact.job_title    = params.job_title
            if params.email is not None and ContactEmailAddress:
                existing_emails = [e for e in (contact.email_addresses or [])
                                   if getattr(e, "label", getattr(e, "name", "")) != "EmailAddress1"]
                try:
                    existing_emails.insert(0, ContactEmailAddress(label="EmailAddress1", email=params.email))
                except TypeError:
                    existing_emails.insert(0, ContactEmailAddress(name="EmailAddress1", email_address=params.email))
                contact.email_addresses = existing_emails
            if params.mobile_phone is not None and PhoneNumber:
                existing_phones = [p for p in (contact.phone_numbers or [])
                                   if getattr(p, "label", getattr(p, "type", "")) != "MobilePhone"]
                try:
                    existing_phones.append(PhoneNumber(label="MobilePhone", phone_number=params.mobile_phone))
                except TypeError:
                    existing_phones.append(PhoneNumber(number=params.mobile_phone, type="MobilePhone"))
                contact.phone_numbers = existing_phones
            if params.business_phone is not None and PhoneNumber:
                existing_phones = [p for p in (contact.phone_numbers or [])
                                   if getattr(p, "label", getattr(p, "type", "")) != "BusinessPhone"]
                try:
                    existing_phones.append(PhoneNumber(label="BusinessPhone", phone_number=params.business_phone))
                except TypeError:
                    existing_phones.append(PhoneNumber(number=params.business_phone, type="BusinessPhone"))
                contact.phone_numbers = existing_phones
            contact.save()
            return contact

        contact = await _run_sync(_update)
        return json.dumps({"status": "updated", "id": contact.id, "display_name": contact.display_name}, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="exchange_delete_contact",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}
)
async def exchange_delete_contact(params: DeleteContactInput) -> str:
    """Delete a contact from the Exchange contacts folder.

    Args:
        params.contact_id: Exchange item ID of the contact to delete

    Returns:
        JSON confirmation.
    """
    try:
        account = _get_account()

        def _delete():
            contact = account.contacts.get(id=params.contact_id)
            contact.delete()

        await _run_sync(_delete)
        return json.dumps({"status": "deleted", "contact_id": params.contact_id})
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Entry point
#
# Local/stdio: `python server.py` with no PORT set (unchanged default).
# Remote (e.g. Railway): PORT is injected automatically. We serve Streamable
# HTTP behind a bearer-token check, since stdio has no transport-level auth
# of its own but a publicly reachable HTTP endpoint needs one.
#
# NOTE: `mcp.streamable_http_app()` is the FastMCP method name as of the
# `mcp` SDK's current streamable-http support. If this errors on startup,
# run `python -c "from mcp.server.fastmcp import FastMCP; print([m for m in dir(FastMCP) if 'app' in m])"`
# against the installed version to find the current method name.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = os.environ.get("PORT")
    if port:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse, PlainTextResponse
        from starlette.routing import Route, Mount
        from contextlib import asynccontextmanager

        AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
        if not AUTH_TOKEN:
            raise RuntimeError(
                "MCP_AUTH_TOKEN is not set. Refusing to start a remote (Streamable "
                "HTTP) deployment without an auth token -- this connector exposes "
                "real mailbox/calendar data, and running unauthenticated would leave "
                "it open to anyone with the URL. Set MCP_AUTH_TOKEN and redeploy."
            )

        class BearerAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.headers.get("authorization", "") != f"Bearer {AUTH_TOKEN}":
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
                return await call_next(request)

        async def health(request):
            # Deliberately unauthenticated: Railway's healthcheck has no way
            # to send the bearer token, and everything under mcp_app requires
            # it. This route is a sibling of mcp_app (not mounted under it),
            # so it can't become an auth bypass for anything else.
            return PlainTextResponse("ok")

        mcp_app = mcp.streamable_http_app()
        mcp_app.add_middleware(BearerAuthMiddleware)

        @asynccontextmanager
        async def combined_lifespan(_app):
            # Mount() does not forward ASGI lifespan events to the mounted
            # sub-app, and mcp_app's session manager only starts inside its
            # own lifespan (this is what actually starts its task group --
            # without it every /mcp request 500s with "Task group is not
            # initialized"). Manually entering it here on the outer app's
            # startup/shutdown keeps that working exactly as before.
            async with mcp_app.router.lifespan_context(mcp_app):
                yield

        app = Starlette(
            routes=[
                Route("/health", health),
                Mount("/", app=mcp_app),
            ],
            lifespan=combined_lifespan,
        )
        uvicorn.run(app, host="0.0.0.0", port=int(port))
    else:
        mcp.run(transport="stdio")
