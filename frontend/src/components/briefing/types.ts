// Shared briefing types used by Homefeed (Today strip) and Productivity
// (full Email + Calendar tabs). Extracted from pages/Homefeed.tsx in UC-39
// so both surfaces consume one definition.

export interface EmailSummary {
    thread_id: string;
    subject: string;
    sender: string;
    snippet: string;
    date: string;
    is_unread: boolean;
    // How you were addressed: "direct" (To) or "cc". Distribution-list / bulk
    // mail is dropped server-side, so the inbox only shows mail aimed at you.
    addressing?: 'direct' | 'cc' | 'list';
    // Lightweight LLM triage flag — true when Cubie judged this email likely to
    // need your personal attention. Renders a "May be important" badge.
    may_be_important?: boolean;
    importance_unavailable?: boolean;
}

export interface Attendee {
    email: string;
    display_name?: string;
    is_organizer: boolean;
}

export interface EventAttachment {
    file_id: string;
    title: string;
    mime_type: string;
    file_url: string;
}

export interface CalendarEvent {
    title: string;
    start: string;
    end: string;
    all_day: boolean;
    event_id?: string;
    description?: string;
    location?: string;
    conference_url?: string;
    attendees: Attendee[];
    attachments: EventAttachment[];
    // Your own RSVP. "none" means the question doesn't apply — a solo event or
    // one you organized. Older cached payloads may omit it.
    rsvp?: 'accepted' | 'tentative' | 'declined' | 'needs_action' | 'none';
}

export interface FreeSlot {
    date: string;
    slots: string[];
}

export interface Briefing {
    generated_at: string;
    emails: EmailSummary[];
    events: CalendarEvent[];
    free_slots: FreeSlot[];
}

// Drive recent-activity item — powers the Productivity → Documents tab.
// Shape mirrors backend.routers.drive.DriveActivityItem.
export interface DriveActivityItem {
    id: string;
    name: string;
    mime_type: string;
    modified_time?: string;
    created_time?: string;
    last_modifying_user_email?: string;
    last_modifying_user_name?: string;
    is_shared: boolean;
    is_owned_by_me: boolean;
    web_view_link?: string;
    viewed_by_me_time?: string;
}

export interface DriveActivityResponse {
    days_back: number;
    items: DriveActivityItem[];
}
