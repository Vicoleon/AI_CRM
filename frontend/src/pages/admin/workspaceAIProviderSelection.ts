/** Resolve a workload/provider pair to one the server catalog actually supports. */
export function compatibleRouteProvider(
    catalog: Record<string, Record<string, string[]>>,
    capability: string,
    requested: string,
): string {
    const eligible = Object.entries(catalog).filter(([, models]) =>
        Object.values(models).some(capabilities => capabilities.includes(capability)));
    if (eligible.some(([name]) => name === requested)) return requested;
    if (eligible.some(([name]) => name === 'vertex_managed')) return 'vertex_managed';
    return eligible[0]?.[0] ?? '';
}
