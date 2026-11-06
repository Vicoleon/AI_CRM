import { clearSupportToken, readSupportToken } from '../brand/storage.ts';

/** A home login/logout in another tab changes the bearer this tab would send.
 *  End any support session in this tab and clear its profile before the next
 *  request can show data under a mismatched identity. */
export function syncHomeTokenStorageEvent(
    event: Pick<StorageEvent, 'key' | 'storageArea'>,
    homeStorage: Storage | null,
    supportStorage: Storage | null,
    currentToken: string | null,
    readNextToken: () => string | null,
    onSessionChange: (nextToken: string | null) => void,
): boolean {
    if (!homeStorage || event.storageArea !== homeStorage || (event.key !== 'token' && event.key !== null)) {
        return false;
    }
    const hadSupportSession = readSupportToken(supportStorage) !== null;
    clearSupportToken(supportStorage);
    const nextToken = readNextToken();
    if (nextToken === currentToken && !hadSupportSession) return false;
    onSessionChange(nextToken);
    return true;
}
