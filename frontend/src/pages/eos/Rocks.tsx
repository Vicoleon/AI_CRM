import React, { useEffect, useState } from 'react';
import client from '../../api/client';
import { useVoiceChanges } from '../../components/voice/useVoiceChanges';
import type { PersonLite, TeamScope } from './_utils';
import { RocksView, pickDefaultQuarterFilter } from '../../components/eos/RocksView';
import type { Rock, RockChange, Quarter } from '../../components/eos/RocksView';

/**
 * Standalone Rocks page. Thin host: it fetches the team's rocks + people +
 * quarters + meeting scopes, owns the `rocks` array, computes the default
 * quarter to open on, and reconciles the write results the shared
 * {@link RocksView} reports back. All rendering + the create/edit modal + the
 * rock-detail modal live in RocksView, which the in-meeting L10 "Rock Review"
 * segment reuses.
 */
export const Rocks: React.FC = () => {
    const [rocks, setRocks] = useState<Rock[]>([]);
    const [people, setPeople] = useState<PersonLite[]>([]);
    const [quarters, setQuarters] = useState<Quarter[]>([]);
    const [teamScopes, setTeamScopes] = useState<TeamScope[]>([]);
    // Which quarter the list should open on — the current quarter (or
    // the latest with rocks). Computed once from the freshly-loaded data and
    // handed to RocksView so the page lands on the cycle in play with no flash
    // of the full "All quarters" backlog.
    const [initialQuarterFilter, setInitialQuarterFilter] = useState<string | undefined>(
        undefined,
    );
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    // Reloads when a voice command changes something on this page.
    const voiceChanges = useVoiceChanges('/eos/rocks');

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const [rRes, pRes, qRes, sRes] = await Promise.all([
                    // surface=rocks: the people who keep every team's Rocks get every
                    // team's owners and teams here (never the Exec team); every
                    // other page asks without it.
                    client.get<Rock[]>('/api/eos/rocks/'),
                    client.get<PersonLite[]>('/api/eos/people/', {
                        params: { surface: 'rocks' },
                    }),
                    client.get<Quarter[]>('/api/eos/quarters/'),
                    client.get<{ scopes: TeamScope[] }>('/api/eos/me/meeting-scopes', {
                        params: { surface: 'rocks' },
                    }),
                ]);
                if (cancelled) return;
                setRocks(rRes.data);
                setPeople(pRes.data);
                setQuarters(qRes.data);
                setTeamScopes(sRes.data.scopes ?? []);
                setInitialQuarterFilter(pickDefaultQuarterFilter(qRes.data, rRes.data));
                setError(null);
            } catch (err) {
                if (!cancelled) setError(err instanceof Error ? err.message : 'Unknown error');
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [voiceChanges]);

    const applyChange = (c: RockChange) => {
        setRocks(prev => {
            switch (c.type) {
                case 'upsert':
                    return prev.some(r => r.id === c.rock.id)
                        ? prev.map(r => (r.id === c.rock.id ? c.rock : r))
                        : [...prev, c.rock];
                case 'remove':
                    return prev.filter(r => r.id !== c.id);
                case 'set':
                    return prev.map(r => (r.id === c.id ? { ...r, ...c.patch } : r));
            }
        });
    };

    return (
        <RocksView
            mode="page"
            rocks={rocks}
            people={people}
            quarters={quarters}
            teamScopes={teamScopes}
            initialQuarterFilter={initialQuarterFilter}
            loading={loading}
            error={error}
            onChange={applyChange}
        />
    );
};
