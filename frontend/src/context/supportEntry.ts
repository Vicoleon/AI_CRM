/** Finish a support Enter only if the home session that requested it is still
 *  active. A delayed response after another tab logs in or out must never
 *  install the old actor's support bearer in this tab. */
export async function completeSupportEntry({
    result,
    isCurrentHomeSession,
    isTokenValid,
    storeToken,
    activate,
    revoke,
}: {
    result: Promise<{ token?: string } | null>;
    isCurrentHomeSession: () => boolean;
    isTokenValid: (token: string) => boolean;
    storeToken: (token: string) => boolean;
    activate: (token: string) => void;
    revoke: (token: string) => Promise<void>;
}): Promise<void> {
    const token = (await result)?.token;
    const revokeAbandoned = async () => {
        if (!token) return;
        try {
            await revoke(token);
        } catch {
            // The browser did not retain the bearer, so it cannot be reused
            // here. The short server-side support session still expires.
            console.error('Could not revoke abandoned support session.');
        }
    };

    if (!isCurrentHomeSession()) {
        await revokeAbandoned();
        throw new Error('Your home session changed while starting support. Try again.');
    }
    if (!token || !isTokenValid(token) || !storeToken(token)) {
        await revokeAbandoned();
        throw new Error('Could not start the support session in this browser.');
    }
    activate(token);
}
