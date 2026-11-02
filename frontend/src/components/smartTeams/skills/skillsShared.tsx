// Shared chrome for the ST-8 skills screens. (Pure helpers live in
// `skillsFormat.ts` — the lint rule keeps components and non-components apart.)
//
// THE ONE RULE THIS FILE EXISTS TO ENFORCE
// ----------------------------------------
// A skill somebody DECLARED and a skill Cubie GUESSED must never look alike. The
// failure mode is not cosmetic: a person who sees Cubie's guess rendered in the
// same chip as their own claim will read it as something they said, and the next
// thing that happens is a colleague being staffed on the strength of it. So the
// two are separated three ways over — a different section, a different card
// shape, and the badge below — and the badge is the smallest of the three, not
// the whole distinction.
//
// `PrivateSkill.origin` is the server's `skills.SOURCE_*` vocabulary and the ONLY
// thing that says which is which. It is never inferred from anything else: a
// confirmed suggestion keeps `origin: 'inferred'` on purpose, because the row's
// history is that Cubie suggested it and the person agreed, and rewriting the
// provenance on confirmation would erase the only record of where it came from.
import React from 'react';
import { FileText, MessageSquare, Sparkles, User } from 'lucide-react';

import type { SkillOrigin } from '../../../api/smartTeams';

/** Where a CONFIRMED skill came from. Only ever rendered on a confirmed row.
 *
 *  There is deliberately no variant for a pending suggestion: a suggestion is
 *  not a skill with a different label on it, it is a question, and it lives in
 *  its own section with its own card. Giving it a badge in this family would
 *  invite it into the confirmed list. */
export const OriginBadge: React.FC<{ origin: SkillOrigin }> = ({ origin }) => {
    if (origin === 'inferred') {
        return (
            <span
                className="inline-flex flex-shrink-0 items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-800"
                title="Cubie suggested this from your own internal activity, and you confirmed it."
            >
                <Sparkles size={11} />
                Cubie suggested · you confirmed
            </span>
        );
    }
    if (origin === 'manager') {
        // In the server's vocabulary, written by no endpoint today. Rendered
        // honestly rather than assumed impossible: silently drawing it as "you
        // added this" would misattribute a claim.
        return (
            <span className="inline-flex flex-shrink-0 items-center gap-1 rounded-full border border-gray-300 bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-700">
                <User size={11} />
                Added by a manager
            </span>
        );
    }
    return (
        <span
            className="inline-flex flex-shrink-0 items-center gap-1 rounded-full border border-brand/30 bg-brand-tint px-2 py-0.5 text-[11px] font-medium text-brand-hover"
            title="You added this yourself."
        >
            <User size={11} />
            You added this
        </span>
    );
};

/** The icon for one citation's `kind`. An unknown kind gets the neutral one
 *  rather than nothing — the sweep owns that vocabulary and may grow it. */
export const EvidenceKindIcon: React.FC<{ kind: string }> = ({ kind }) => {
    if (kind === 'meeting') return <MessageSquare size={13} />;
    return <FileText size={13} />;
};

export const SectionCard: React.FC<{
    title: string;
    icon: React.ReactNode;
    count?: number;
    subtitle?: React.ReactNode;
    right?: React.ReactNode;
    tone?: 'plain' | 'suggestion' | 'quiet';
    children: React.ReactNode;
}> = ({ title, icon, count, subtitle, right, tone = 'plain', children }) => {
    const shell =
        tone === 'suggestion'
            ? 'border-indigo-200 bg-indigo-50/40'
            : tone === 'quiet'
              ? 'border-gray-200 bg-gray-50'
              : 'border-gray-200 bg-white';
    return (
        <section className={`rounded-lg border p-5 ${shell}`}>
            <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h2 className="flex items-center gap-2 text-base font-semibold text-gray-900">
                        <span className="text-brand">{icon}</span>
                        {title}
                        {typeof count === 'number' && (
                            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700">
                                {count}
                            </span>
                        )}
                    </h2>
                    {subtitle && (
                        <div className="mt-1 text-sm leading-relaxed text-gray-600">{subtitle}</div>
                    )}
                </div>
                {right}
            </div>
            {children}
        </section>
    );
};

/** An empty section that is a legitimate, finished answer — not a failure and
 *  not a to-do. Nobody has skills on day one, and the sweep may honestly have
 *  nothing to suggest; both of those are ordinary, so neither gets a warning
 *  colour, a retry button or a nudge. */
export const QuietEmpty: React.FC<{ title: string; children?: React.ReactNode }> = ({
    title,
    children,
}) => (
    <div className="rounded-lg border border-dashed border-gray-300 bg-white/60 p-5">
        <p className="text-sm font-medium text-gray-700">{title}</p>
        {children && (
            <div className="mt-1 space-y-1 text-xs leading-relaxed text-gray-500">{children}</div>
        )}
    </div>
);
