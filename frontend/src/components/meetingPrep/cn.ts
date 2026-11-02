import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/** Tailwind class join that lets a later class override an earlier one. */
export function cn(...inputs: ClassValue[]): string {
    return twMerge(clsx(inputs));
}
