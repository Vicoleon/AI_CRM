// The copilot boundary, said out loud.
//
// Compare (ST-7) and Team Builder (ST-9) are the two Smart Teams surfaces that
// also need the copilot — `backend/routers/smart_teams_teams.py` stacks
// `require_full_app_access` on top of the Smart Teams gate for exactly these
// two. Everyone has the copilot by default; an admin narrows it in Admin →
// Feature Access.
//
// The pages check `canAccessFullApp` before rendering, so in normal operation
// nobody sees this. It exists for the case that check cannot cover: the client
// reads the copilot verdict once from /api/users/me, and when an admin changes
// Feature Access in the meantime, the client flag says yes, the page renders,
// and the API answers 403. That 403 has to render as a sentence a person can
// act on rather than as "something went wrong", which is what a generic error
// box would say.
//
// Lives in `compare/` because Compare needed it first; the Team Builder page
// imports the same component rather than growing a second copy of the copy.
import React from 'react';
import { Link } from 'react-router-dom';
import { Lock } from 'lucide-react';

export interface CopilotOnlyProps {
    /** What the person was trying to open, for the first sentence. */
    surface: string;
    /** The server's own refusal sentence when this came from a 403
     *  (`"This area is not turned on for you. Ask your admin."`). Rendered
     *  verbatim; omitted when the check was client-side. */
    refusal?: string | null;
}

export const CopilotOnly: React.FC<CopilotOnlyProps> = ({ surface, refusal }) => (
    <div className="rounded-xl border border-gray-200 bg-white p-5">
        <div className="mb-2 flex items-center gap-2">
            <Lock size={16} aria-hidden="true" className="text-gray-500" />
            <h2 className="text-base font-semibold text-gray-900">
                {surface} is not available to you yet
            </h2>
        </div>
        <p className="text-sm leading-relaxed text-gray-700">
            {surface} is not turned on for you; your admin can turn it on. Everything
            else in Smart Teams is yours either way — the Work Style Interview, your
            own working guide, and your own skills.
        </p>
        {refusal && (
            <p className="mt-3 rounded-lg border border-gray-200 bg-gray-50 p-3 text-sm leading-relaxed text-gray-700">
                {refusal}
            </p>
        )}
        <Link
            to="/smart-teams"
            className="mt-4 inline-flex items-center rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 shadow-sm transition-colors hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2"
        >
            Back to My Work Style
        </Link>
    </div>
);

export default CopilotOnly;
