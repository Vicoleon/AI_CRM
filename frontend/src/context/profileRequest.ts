/** Apply an async /me result only to the session that started it. The token
 *  check catches storage changes; the generation check also catches a switch
 *  away and back to the same home token. */
export async function loadCurrentProfile<T>({
    token,
    load,
    isCurrentGeneration,
    activeToken,
    onProfile,
    onUnauthorized,
    onError,
    onSettled,
}: {
    token: string;
    load: () => Promise<T>;
    isCurrentGeneration: () => boolean;
    activeToken: () => string | null;
    onProfile: (profile: T) => void;
    onUnauthorized: () => void;
    onError: (error: unknown) => void;
    onSettled: () => void;
}): Promise<void> {
    if (!isCurrentGeneration() || activeToken() !== token) return;
    try {
        const profile = await load();
        if (isCurrentGeneration() && activeToken() === token) onProfile(profile);
    } catch (error) {
        if (!isCurrentGeneration()) return;
        if ((error as { response?: { status?: number } }).response?.status === 401) {
            // Axios has already removed the rejected token. The current
            // request still needs to restore the saved home session.
            onUnauthorized();
        } else if (activeToken() === token) {
            onError(error);
        }
    } finally {
        if (isCurrentGeneration() && activeToken() === token) onSettled();
    }
}
