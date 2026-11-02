export interface WorkspaceIdentity {
    primary_domain: string;
    domains: string[];
    modules: { core: boolean; eos: boolean; sales_coach: boolean };
}

/** Treat a partial or older response as unavailable rather than guessing access. */
export function parseWorkspaceIdentity(value: unknown): WorkspaceIdentity | null {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
    const identity = value as Record<string, unknown>;
    const domains = identity.domains;
    const modules = identity.modules;
    if (
        typeof identity.primary_domain !== 'string' ||
        !Array.isArray(domains) ||
        !domains.every((domain: unknown) => typeof domain === 'string') ||
        typeof modules !== 'object' ||
        modules === null ||
        Array.isArray(modules)
    ) return null;

    const moduleFlags = modules as Record<string, unknown>;
    if (
        typeof moduleFlags.core !== 'boolean' ||
        typeof moduleFlags.eos !== 'boolean' ||
        typeof moduleFlags.sales_coach !== 'boolean'
    ) return null;

    return {
        primary_domain: identity.primary_domain,
        domains,
        modules: {
            core: moduleFlags.core,
            eos: moduleFlags.eos,
            sales_coach: moduleFlags.sales_coach,
        },
    };
}
