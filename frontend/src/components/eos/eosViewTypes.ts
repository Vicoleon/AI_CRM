/**
 * Shared types for the reusable EOS views (`RocksView`, `IssuesView`,
 * `TodosView`) that back BOTH the standalone pages and the in-meeting L10
 * segments. Kept in one place so the three views agree on the meeting seam.
 */

/**
 * Present only when a view renders inside an L10 meeting segment. Lets the view
 * stamp items it creates with the meeting's team (department or Exec) and link
 * them to this L10, exactly as the L10 host's `meetingTeam()` helper does, so
 * they resurface in the team's future meetings instead of being keyed to one
 * meeting instance.
 */
export interface MeetingContext {
    l10MeetingId: string;
    /** Spread verbatim into create payloads — `{ is_exec: true }` for an Exec
     * meeting, else `{ department_name }`. */
    team: { department_name?: string | null; is_exec?: boolean };
}
