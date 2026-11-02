import client from './client';

/** One entry in the "/" menu of the Ask Cubie chat box. */
export interface SlashEntry {
    slug: string;
    name: string;
    description: string;
}

export interface SlashMenu {
    skills: SlashEntry[];
    templates: SlashEntry[];
}

/** The signed-in person's saved meeting skills and templates. Creating,
 *  changing and running them all happen in the conversation itself. */
export async function fetchSlashMenu(): Promise<SlashMenu> {
    const { data } = await client.get<SlashMenu>('/api/meeting-skills/menu');
    return {
        skills: Array.isArray(data?.skills) ? data.skills : [],
        templates: Array.isArray(data?.templates) ? data.templates : [],
    };
}
