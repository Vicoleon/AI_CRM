import axios from 'axios';

import client from '../../api/client';
import { dropRejectedToken } from '../../brand/storage';
import type { WorkspaceRequestIdentity } from './workspaceOperationsRequest';

/** Send one workspace's form using the session that loaded it. */
export function pinnedWorkspaceClient(identity: WorkspaceRequestIdentity) {
    const request = axios.create({ timeout: client.defaults.timeout });
    request.interceptors.request.use(config => {
        config.headers.Authorization = `Bearer ${identity.token}`;
        Object.assign(config.headers, identity.headers);
        config.headers['X-Request-ID'] = crypto.randomUUID().slice(0, 8);
        return config;
    });
    request.interceptors.response.use(response => response, error => {
        if (error?.response?.status === 401) {
            const rejected = dropRejectedToken(identity.token);
            if (rejected === 'support') window.location.assign('/');
            else if (rejected === 'home' && window.location.pathname !== '/login') window.location.assign('/login');
        }
        return Promise.reject(error);
    });
    return request;
}
