# RingCentral ↔ Odoo 19 Integration — Architecture

## Overview

This module integrates RingCentral phone calls with Odoo 19 (Odoo.sh), providing:
- Automatic call logging against Odoo contacts (res.partner)
- Call recording attachments on contact/ticket records
- Call transcript attachments (via RingSense AI or fallback to Anthropic)
- Sentiment analysis with automatic ticket creation/priority escalation

---

## Data Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                        RINGCENTRAL CLOUD                            │
│                                                                     │
│  ┌──────────┐   ┌──────────────┐   ┌────────────────┐              │
│  │ Call PBX  │──▶│ Call Log API  │──▶│ Webhook Events │──────┐      │
│  └──────────┘   └──────────────┘   └────────────────┘      │      │
│                                                              │      │
│  ┌───────────────┐   ┌──────────────────┐                   │      │
│  │ Recording API  │   │ RingSense AI     │                   │      │
│  │ (download mp3) │   │ (transcript +    │                   │      │
│  └───────┬───────┘   │  sentiment)      │                   │      │
│          │            └────────┬─────────┘                   │      │
└──────────┼─────────────────────┼─────────────────────────────┼──────┘
           │                     │                             │
           ▼                     ▼                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      MIDDLEWARE (n8n / Custom)                       │
│                                                                     │
│  ┌─────────────────┐   ┌──────────────────┐   ┌────────────────┐   │
│  │ Webhook Receiver │──▶│ Data Transformer │──▶│ Odoo API Client│   │
│  │                  │   │                  │   │  (JSON-RPC)    │   │
│  │ Events:          │   │ • Match caller   │   └───────┬────────┘   │
│  │ • call.started   │   │   to res.partner │           │            │
│  │ • call.ended     │   │ • Download       │           │            │
│  │ • call.missed    │   │   recording      │           │            │
│  │ • voicemail      │   │ • Get transcript │           │            │
│  └─────────────────┘   │ • Run sentiment  │           │            │
│                         │   analysis       │           │            │
│                         └──────────────────┘           │            │
└────────────────────────────────────────────────────────┼────────────┘
                                                         │
                                                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        ODOO 19 (Odoo.sh)                            │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                  ringcentral_odoo module                      │   │
│  │                                                              │   │
│  │  ┌──────────────────┐   ┌──────────────────┐                │   │
│  │  │ rc.call.log      │   │ rc.config        │                │   │
│  │  │ (Call Records)   │   │ (Settings)       │                │   │
│  │  │                  │   │                  │                │   │
│  │  │ • partner_id     │   │ • api_key        │                │   │
│  │  │ • direction      │   │ • api_secret     │                │   │
│  │  │ • duration       │   │ • webhook_secret │                │   │
│  │  │ • recording_url  │   │ • sentiment_     │                │   │
│  │  │ • transcript     │   │   threshold      │                │   │
│  │  │ • sentiment_score│   │ • auto_ticket    │                │   │
│  │  │ • ticket_id      │   └──────────────────┘                │   │
│  │  └──────┬───────────┘                                       │   │
│  │         │                                                    │   │
│  │         │ on_negative_sentiment()                            │   │
│  │         ▼                                                    │   │
│  │  ┌──────────────────┐   ┌──────────────────┐                │   │
│  │  │ helpdesk.ticket  │   │ res.partner      │                │   │
│  │  │ (Auto-created)   │   │ (Call history     │                │   │
│  │  │                  │   │  on contact)      │                │   │
│  │  │ • High priority  │   │                  │                │   │
│  │  │ • Call transcript │   │ • call_ids       │                │   │
│  │  │ • Recording link │   │ • call_count     │                │   │
│  │  └──────────────────┘   │ • last_call_date │                │   │
│  │                          └──────────────────┘                │   │
│  │                                                              │   │
│  │  ┌──────────────────────────────────────────┐                │   │
│  │  │ Controllers (Webhook Endpoints)           │                │   │
│  │  │                                          │                │   │
│  │  │ POST /ringcentral/webhook/call           │                │   │
│  │  │ POST /ringcentral/webhook/voicemail      │                │   │
│  │  │ GET  /ringcentral/webhook/validate       │                │   │
│  │  └──────────────────────────────────────────┘                │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Sentiment Analysis Pipeline

```
Call Ends
    │
    ▼
Recording Available? ──No──▶ Log call without transcript
    │
   Yes
    │
    ▼
RingSense AI Available? ──No──▶ Download recording ──▶ Whisper/Anthropic transcription
    │
   Yes
    │
    ▼
Get transcript + sentiment from RingSense
    │
    ▼
Sentiment Score < Threshold? ──No──▶ Log call normally
    │
   Yes (Negative)
    │
    ▼
┌─────────────────────────────────┐
│ Auto-create Helpdesk Ticket     │
│ • Priority: High/Urgent         │
│ • Attach transcript             │
│ • Attach recording              │
│ • Link to contact               │
│ • Tag: "negative-call-sentiment"│
└─────────────────────────────────┘
    │
    ▼
Existing open ticket for contact? ──Yes──▶ Escalate priority on existing ticket
    │
   No
    │
    ▼
Create new ticket
```

---

## Module File Structure

```
ringcentral_odoo/
├── __init__.py
├── __manifest__.py
├── ARCHITECTURE.md
│
├── models/
│   ├── __init__.py
│   ├── rc_call_log.py          # Call log records
│   ├── rc_config.py            # Module settings/credentials
│   ├── res_partner.py          # Extend contacts with call fields
│   └── helpdesk_ticket.py      # Extend tickets with call fields
│
├── views/
│   ├── rc_call_log_views.xml   # Call log list/form views
│   ├── rc_config_views.xml     # Settings page
│   ├── res_partner_views.xml   # Call tab on contact form
│   └── helpdesk_ticket_views.xml # Call info on ticket form
│
├── controllers/
│   ├── __init__.py
│   └── webhook.py              # Webhook endpoints for RingCentral
│
├── wizards/
│   ├── __init__.py
│   └── rc_manual_sync.py       # Manual call sync wizard
│
├── security/
│   ├── ir.model.access.csv     # Access rights
│   └── rc_security.xml         # Record rules & groups
│
├── data/
│   ├── rc_cron.xml             # Scheduled actions (polling fallback)
│   ├── rc_mail_templates.xml   # Email templates for escalations
│   └── rc_helpdesk_tags.xml    # Default tags
│
└── static/
    └── description/
        └── icon.png
```

---

## Models Detail

### rc.call.log (Core)
| Field              | Type       | Description                              |
|--------------------|------------|------------------------------------------|
| name               | Char       | Auto-generated: "CALL-000001"            |
| partner_id         | Many2one   | res.partner — matched by phone number    |
| rc_call_id         | Char       | RingCentral call session ID (unique key) |
| direction          | Selection  | inbound / outbound / missed              |
| caller_number      | Char       | Caller phone number                      |
| callee_number      | Char       | Callee phone number                      |
| start_time         | Datetime   | Call start timestamp                     |
| end_time           | Datetime   | Call end timestamp                       |
| duration           | Integer    | Duration in seconds                      |
| state              | Selection  | new / processed / failed                 |
| recording_url      | Char       | RingCentral recording URL                |
| recording_attachment_id | Many2one | ir.attachment — downloaded recording   |
| transcript         | Text       | Full call transcript                     |
| transcript_attachment_id | Many2one | ir.attachment — transcript file        |
| sentiment_score    | Float      | -1.0 (negative) to 1.0 (positive)       |
| sentiment_label    | Selection  | positive / neutral / negative            |
| ticket_id          | Many2one   | helpdesk.ticket — auto-created ticket    |
| user_id            | Many2one   | res.users — Odoo user who handled call   |
| rc_extension_id    | Char       | RingCentral extension that handled call  |
| notes              | Text       | Agent notes                              |

### rc.config (Settings)
| Field                    | Type      | Description                            |
|--------------------------|-----------|----------------------------------------|
| rc_client_id             | Char      | RingCentral OAuth client ID            |
| rc_client_secret         | Char      | RingCentral OAuth client secret        |
| rc_account_id            | Char      | RingCentral account ID (~)             |
| rc_webhook_secret        | Char      | Webhook verification token             |
| rc_jwt_token             | Char      | JWT for server-to-server auth          |
| rc_access_token          | Char      | Current OAuth access token             |
| rc_token_expiry          | Datetime  | Token expiration time                  |
| sentiment_provider       | Selection | ringsense / anthropic / disabled       |
| anthropic_api_key        | Char      | Anthropic API key (if using Claude)    |
| sentiment_threshold      | Float     | Score below which = negative (default -0.3) |
| auto_create_ticket       | Boolean   | Auto-create ticket on negative call    |
| auto_escalate_ticket     | Boolean   | Auto-escalate existing open tickets    |
| escalation_priority      | Selection | 0/1/2/3 — priority for auto tickets   |
| helpdesk_team_id         | Many2one  | helpdesk.team — target team            |
| sync_recordings          | Boolean   | Download & attach recordings           |
| sync_transcripts         | Boolean   | Fetch & attach transcripts             |
| polling_enabled          | Boolean   | Enable cron-based polling fallback     |
| polling_interval         | Integer   | Minutes between poll syncs             |

### res.partner (Extension)
| Field          | Type     | Description                          |
|----------------|----------|--------------------------------------|
| rc_call_ids    | One2many | rc.call.log records for this contact |
| rc_call_count  | Integer  | Computed: total calls                |
| rc_last_call   | Datetime | Computed: most recent call           |

### helpdesk.ticket (Extension)
| Field           | Type     | Description                         |
|-----------------|----------|-------------------------------------|
| rc_call_ids     | One2many | Related call log records            |
| rc_call_count   | Integer  | Computed: calls linked to ticket    |
| rc_escalated    | Boolean  | Was this escalated by sentiment?    |

---

## Webhook Events & Mapping

| RingCentral Event           | Odoo Action                                           |
|-----------------------------|-------------------------------------------------------|
| `rc.call.session.started`   | Create rc.call.log (state=new)                        |
| `rc.call.session.ended`     | Update rc.call.log (duration, end_time)               |
|                              | → Trigger recording download                          |
|                              | → Trigger transcript fetch                            |
|                              | → Run sentiment analysis                              |
|                              | → Create/escalate ticket if negative                  |
| `rc.call.session.missed`    | Create rc.call.log (direction=missed)                 |
| `rc.voicemail.received`     | Create rc.call.log + attach voicemail                 |

---

## API Endpoints (Odoo Controllers)

### POST /ringcentral/webhook/call
Receives call events from RingCentral webhook subscription.
- Validates webhook signature
- Parses event type
- Creates/updates rc.call.log
- Triggers async processing (recording, transcript, sentiment)

### POST /ringcentral/webhook/validate
RingCentral webhook validation endpoint (initial subscription handshake).
- Returns validation token from header

### GET /ringcentral/api/calls
Internal API for frontend widgets (call history on contact).
- Requires Odoo session authentication

---

## Phone Number Matching Logic

```
Incoming call from: +27821234567

1. Normalize number → strip spaces, dashes, parentheses
2. Search res.partner:
   a. Exact match on phone or mobile field
   b. Fuzzy match: try with/without country code (+27 → 0)
   c. Try last 10 digits match
   d. Search phone field in all partner phone-type fields
3. If no match:
   a. Create rc.call.log with partner_id = False
   b. Show "Unknown Caller" in call log
   c. Option to manually link later via wizard
```

---

## Cron Jobs (Polling Fallback)

| Cron                        | Interval | Action                                    |
|-----------------------------|----------|-------------------------------------------|
| rc_sync_calls               | 5 min    | Poll RingCentral Call Log API             |
| rc_sync_recordings          | 10 min   | Download pending recordings               |
| rc_sync_transcripts         | 10 min   | Fetch pending transcripts                 |
| rc_refresh_token            | 30 min   | Refresh OAuth access token                |
| rc_retry_failed             | 60 min   | Retry failed processing                   |

---

## Security

### Groups
- **RingCentral / User** — View call logs, own records
- **RingCentral / Manager** — All call logs, settings, manual sync

### Access Rights
| Model       | User         | Manager     |
|-------------|--------------|-------------|
| rc.call.log | Read         | CRUD        |
| rc.config   | —            | Read/Write  |

### Webhook Security
- Validate `Verification-Token` header on all inbound webhooks
- Rate limiting on webhook endpoints
- HMAC signature verification (if available)
