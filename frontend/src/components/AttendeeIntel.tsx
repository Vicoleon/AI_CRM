import React from 'react';
import { Linkedin, ExternalLink, MessageSquareQuote, Radar, Ban, Sparkle } from 'lucide-react';
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * UC-47 attendee intelligence — the researched public record for one external
 * meeting attendee. Mirrors `rubik_core/people/schemas.AttendeeIntel`.
 */
export interface AttendeeActivity {
  kind: string;
  posted_at: string;
  summary: string;
  theme: string;
  url: string;
}

export interface AttendeeProfileInfo {
  name: string;
  linkedin_url: string;
  headline: string;
  current_title: string;
  current_company: string;
  tenure: string;
  location: string;
  career_summary: string;
  credentials: string[];
}

export interface AttendeePersona {
  communication_style: string;
  decision_style: string;
  priorities: string[];
  posts_about: string[];
  conversation_openers: string[];
  avoid: string[];
  confidence: 'high' | 'medium' | 'low' | string;
  basis: string;
}

export interface AttendeeIntel {
  email: string;
  name: string;
  profile: AttendeeProfileInfo;
  activity: AttendeeActivity[];
  persona: AttendeePersona;
  sources: string[];
  provider: string;
  researched_at: string;
  error: string;
}

/** True when this card has anything worth rendering. */
export function hasIntelContent(intel?: AttendeeIntel | null): boolean {
  if (!intel) return false;
  const p = intel.profile;
  const persona = intel.persona;
  return Boolean(
    intel.activity?.length ||
    p?.linkedin_url || p?.headline || p?.current_title || p?.career_summary ||
    persona?.communication_style || persona?.decision_style ||
    persona?.priorities?.length || persona?.posts_about?.length
  );
}

/** Index researched attendees by lowercased email, for per-person lookups. */
export function indexIntelByEmail(
  intel?: AttendeeIntel[] | null
): Record<string, AttendeeIntel> {
  const out: Record<string, AttendeeIntel> = {};
  for (const person of intel || []) {
    if (person?.email) out[person.email.toLowerCase()] = person;
  }
  return out;
}

/**
 * The confidence band, rendered as a label rather than hidden.
 *
 * The persona read is the one inferred part of the bundle, and a rep about to
 * quote it out loud needs to know whether it rests on a dozen posts or on a job
 * title. Colour carries the same message as the word for anyone scanning.
 */
const ConfidenceBadge: React.FC<{ confidence: string; basis?: string }> = ({ confidence, basis }) => {
  const styles: Record<string, string> = {
    high: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    medium: 'bg-amber-50 text-amber-700 border-amber-200',
    low: 'bg-slate-100 text-slate-500 border-slate-200',
  };
  const label: Record<string, string> = {
    high: 'Well evidenced',
    medium: 'Partly evidenced',
    low: 'Thin evidence',
  };
  const key = ['high', 'medium', 'low'].includes(confidence) ? confidence : 'low';
  return (
    <span
      title={basis ? `Based on: ${basis}` : undefined}
      className={cn(
        'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-[10px] font-bold uppercase tracking-wider shrink-0',
        styles[key]
      )}
    >
      <Radar className="w-3 h-3" />
      {label[key]}
    </span>
  );
};

const Chip: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <span className="inline-flex items-center px-2.5 py-1 rounded-lg bg-slate-100 text-slate-600 text-[11px] font-semibold">
    {children}
  </span>
);

/** A LinkedIn profile link — the "go see for yourself" affordance. */
export const LinkedInLink: React.FC<{ url: string; compact?: boolean }> = ({ url, compact }) => (
  <a
    href={url}
    target="_blank"
    rel="noopener noreferrer"
    onClick={(e) => e.stopPropagation()}
    className={cn(
      'inline-flex items-center gap-1.5 font-bold text-[#0a66c2] hover:underline',
      compact ? 'text-[11px]' : 'text-xs'
    )}
  >
    <Linkedin className={compact ? 'w-3 h-3' : 'w-3.5 h-3.5'} />
    LinkedIn
    <ExternalLink className="w-2.5 h-2.5 opacity-60" />
  </a>
);

const Labelled: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className="space-y-1">
    <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400">{label}</p>
    <div className="text-sm text-slate-600 leading-relaxed">{children}</div>
  </div>
);

/** One attendee: position, how they come across, and what they've been saying. */
const AttendeeIntelCard: React.FC<{ intel: AttendeeIntel }> = ({ intel }) => {
  const { profile, persona, activity } = intel;
  const displayName = intel.name || profile?.name || intel.email;
  const initials = displayName.slice(0, 2).toUpperCase();
  const role = [profile?.current_title, profile?.current_company].filter(Boolean).join(' @ ');

  return (
    <div className="rounded-[1.5rem] border border-slate-200/60 bg-white p-6 space-y-5">
      {/* Identity */}
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-brand to-brand-hover text-white flex items-center justify-center font-black shadow-lg shadow-brand/20 shrink-0">
          {initials}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h4 className="text-lg font-black text-slate-900 leading-tight">{displayName}</h4>
            {profile?.linkedin_url && <LinkedInLink url={profile.linkedin_url} />}
          </div>
          {role && (
            <p className="text-xs text-brand font-bold uppercase tracking-wider mt-1">{role}</p>
          )}
          {profile?.headline && (
            <p className="text-sm text-slate-500 italic mt-1.5 leading-snug">“{profile.headline}”</p>
          )}
          <div className="flex flex-wrap gap-2 mt-3">
            {profile?.tenure && <Chip>{profile.tenure}</Chip>}
            {profile?.location && <Chip>{profile.location}</Chip>}
            {(profile?.credentials || []).slice(0, 4).map((c, i) => (
              <Chip key={i}>{c}</Chip>
            ))}
          </div>
        </div>
        <ConfidenceBadge confidence={persona?.confidence} basis={persona?.basis} />
      </div>

      {profile?.career_summary && (
        <Labelled label="Trajectory">{profile.career_summary}</Labelled>
      )}

      {/* How to talk to them */}
      {(persona?.communication_style || persona?.decision_style) && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 p-4 rounded-2xl bg-slate-50/70 border border-slate-100">
          {persona.communication_style && (
            <Labelled label="Communication style">{persona.communication_style}</Labelled>
          )}
          {persona.decision_style && (
            <Labelled label="How they decide">{persona.decision_style}</Labelled>
          )}
        </div>
      )}

      {!!persona?.posts_about?.length && (
        <Labelled label="Themes they keep returning to">
          <div className="flex flex-wrap gap-2 mt-1">
            {persona.posts_about.map((t, i) => (
              <Chip key={i}>{t}</Chip>
            ))}
          </div>
        </Labelled>
      )}

      {!!persona?.priorities?.length && (
        <Labelled label="What they publicly push for">
          <ul className="space-y-1.5 mt-1">
            {persona.priorities.map((p, i) => (
              <li key={i} className="flex gap-2">
                <span className="mt-1.5 w-1.5 h-1.5 rounded-full bg-brand/30 shrink-0" />
                <span>{p}</span>
              </li>
            ))}
          </ul>
        </Labelled>
      )}

      {/* Their own words — the reason this panel exists */}
      {!!activity?.length && (
        <div className="space-y-2">
          <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400">
            Recent activity ({activity.length})
          </p>
          <ul className="space-y-2.5">
            {activity.map((item, i) => (
              <li key={i} className="rounded-xl border border-slate-100 bg-slate-50/50 p-3">
                <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                  <span className="text-[10px] font-bold uppercase tracking-wider text-brand">
                    {item.kind || 'post'}
                  </span>
                  {item.posted_at && (
                    <span className="text-[10px] text-slate-400 font-semibold">{item.posted_at}</span>
                  )}
                  {item.theme && <Chip>{item.theme}</Chip>}
                </div>
                <p className="text-sm text-slate-600 leading-relaxed">{item.summary}</p>
                {item.url && (
                  <a
                    href={item.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 mt-2 text-[11px] font-bold text-brand hover:underline"
                  >
                    Open source
                    <ExternalLink className="w-2.5 h-2.5" />
                  </a>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {!!persona?.conversation_openers?.length && (
        <div className="rounded-2xl border border-brand/15 bg-brand/5 p-4 space-y-2">
          <p className="text-[10px] font-bold uppercase tracking-wider text-brand flex items-center gap-1.5">
            <MessageSquareQuote className="w-3.5 h-3.5" />
            Openers that would land
          </p>
          <ul className="space-y-2">
            {persona.conversation_openers.map((o, i) => (
              <li key={i} className="text-sm text-slate-700 leading-relaxed flex gap-2">
                <Sparkle className="w-3.5 h-3.5 text-brand mt-0.5 shrink-0" />
                <span>{o}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {!!persona?.avoid?.length && (
        <div className="rounded-2xl border border-amber-200 bg-amber-50/60 p-4 space-y-2">
          <p className="text-[10px] font-bold uppercase tracking-wider text-amber-700 flex items-center gap-1.5">
            <Ban className="w-3.5 h-3.5" />
            Would land badly
          </p>
          <ul className="space-y-1.5">
            {persona.avoid.map((a, i) => (
              <li key={i} className="text-sm text-amber-900/80 leading-relaxed">{a}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Provenance. Every claim above is clickable, so a rep can verify before
          repeating it in the room. */}
      {!!intel.sources?.length && (
        <details className="group">
          <summary className="cursor-pointer text-[11px] font-bold uppercase tracking-wider text-slate-400 hover:text-slate-600">
            {intel.sources.length} source{intel.sources.length === 1 ? '' : 's'}
            {intel.provider ? ` · ${intel.provider.replace('_', ' ')}` : ''}
          </summary>
          <ul className="mt-2 space-y-1">
            {intel.sources.map((s, i) => (
              <li key={i} className="truncate">
                <a
                  href={s}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-[11px] text-slate-500 hover:text-brand hover:underline"
                >
                  {s}
                </a>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
};

/**
 * The Attendee Intelligence panel.
 *
 * Renders only the people research actually found something for, and says
 * plainly who it came up empty on — a name silently missing from the list reads
 * as "not researched", which is the one impression that would make a rep
 * over-trust the cards that ARE here.
 */
export const AttendeeIntelPanel: React.FC<{ intel: AttendeeIntel[] }> = ({ intel }) => {
  const found = (intel || []).filter(hasIntelContent);
  const missing = (intel || []).filter((p) => !hasIntelContent(p));

  if (!found.length && !missing.length) return null;

  return (
    <div className="space-y-4">
      {found.map((person) => (
        <AttendeeIntelCard key={person.email} intel={person} />
      ))}

      {!!missing.length && (
        <p className="text-xs text-slate-400 italic leading-relaxed">
          No public professional presence found for{' '}
          {missing.map((p) => p.name || p.email).join(', ')}. Nothing was inferred about them.
        </p>
      )}
    </div>
  );
};

export default AttendeeIntelPanel;
