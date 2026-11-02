/** A credential form may write only for the identity that loaded its values. */
export function formBelongsToIdentity(formEpoch: number, currentEpoch: number): boolean {
    return formEpoch === currentEpoch;
}
