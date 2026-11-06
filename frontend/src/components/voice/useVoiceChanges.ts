// A count that goes up each time a voice command changes something shown on
// the page at `path` (see announceVoiceChange in api/voiceCommands.ts). A page
// adds it to its fetch effect's dependencies, so a Rock added by voice while
// the Rocks page is open appears without a reload.
import { useEffect, useState } from 'react';

import { VOICE_CHANGED_EVENT } from '../../api/voiceCommands';

export function useVoiceChanges(path: string): number {
    const [count, setCount] = useState(0);
    useEffect(() => {
        const onChange = (e: Event) => {
            if ((e as CustomEvent<{ path?: string }>).detail?.path === path) setCount(n => n + 1);
        };
        window.addEventListener(VOICE_CHANGED_EVENT, onChange);
        return () => window.removeEventListener(VOICE_CHANGED_EVENT, onChange);
    }, [path]);
    return count;
}
