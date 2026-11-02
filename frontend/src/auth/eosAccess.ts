interface EosProfile {
    modules?: { eos?: unknown } | null;
}

export type EosRouteDecision = 'login' | 'loading' | 'allow' | 'help';

/** One profile verdict for both the EOS rail area and every /eos child route. */
export function eosUiGate({
    token,
    user,
    isLoading,
}: {
    token: string | null;
    user: EosProfile | null;
    isLoading: boolean;
}): { showNav: boolean; route: EosRouteDecision } {
    if (!token) return { showNav: false, route: 'login' };
    if (isLoading || !user) return { showNav: false, route: 'loading' };
    const enabled = user.modules?.eos === true;
    return { showNav: enabled, route: enabled ? 'allow' : 'help' };
}
