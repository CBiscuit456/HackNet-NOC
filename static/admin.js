function adminApp() {
    return {
        isLoading: true,
        isAuthenticated: false,
        jwtToken: '',
        password: '',
        targets: [],
        logs: [],
        settings: { global_mute: '0', hud_priorities: '' },
        
        activeView: 'infrastructure', 
        mobileMenuOpen: false,
        
        isSensorModalOpen: false,
        isEditing: false,
        
        isSavingIntegrations: false,
        isScanning: false,
        integrations: {},
        
        searchQuery: '',

        basePriorityOptions: [
            { id: 'compute', label: 'Compute Core', triggers: ['proxmox', 'esxi'] },
            { id: 'storage', label: 'Storage Fleet', triggers: ['truenas', 'synology', 'qnap'] },
            { id: 'docker', label: 'Docker Fleet', triggers: ['portainer'] },
            { id: 'dns', label: 'DNS Sinkhole', triggers: ['pihole'] },
            { id: 'games', label: 'Game Fleet', triggers: ['pterodactyl'] },
            { id: 'media', label: 'Media Services', triggers: ['jellyfin', 'plex'] }
        ],
        hudPriorities: [],

        form: { uid: '', name: '', entity_type: '', target_address: '', port: '', cooldown_interval: 3600, cpu_threshold: 90.0, mem_threshold: 90.0, location: 'Local', parent_uid: '', stack_group: '' },

        get filteredTargets() {
            if (!this.searchQuery) return this.targets;
            const q = this.searchQuery.toLowerCase();
            return this.targets.filter(t => 
                t.entity_name.toLowerCase().includes(q) || 
                (t.target_address || '').toLowerCase().includes(q) ||
                (t.location || '').toLowerCase().includes(q) ||
                t.entity_type.toLowerCase().includes(q)
            );
        },
        
        get potentialParents() {
            return this.targets.filter(t => ['host', 'vm', 'lxc'].includes(t.entity_type) && t.uid !== this.form.uid);
        },
        
        updateStackGroup() {
            if (!this.form.parent_uid) {
                this.form.stack_group = '';
                return;
            }
            const p = this.potentialParents.find(x => x.uid === this.form.parent_uid);
            if (p) this.form.stack_group = p.entity_name;
        },
        
        isPluginActive(trigger) {
            const keys = Object.keys(this.integrations);
            return keys.some(k => k.startsWith(`plugin_${trigger}_`) && this.integrations[k] && this.integrations[k] !== "");
        },
        
        get availablePriorities() {
            return this.basePriorityOptions.filter(opt => 
                opt.triggers.some(t => this.isPluginActive(t))
            );
        },

        isAutoDiscovered() {
            return this.form.uid !== '' && !this.form.uid.startsWith('mnl_');
        },

        getCooldownText(seconds) {
            const s = parseInt(seconds) || 3600;
            if (s === 600) return "10M";
            if (s === 1200) return "20M";
            if (s === 3600) return "1H";
            if (s === 14400) return "4H";
            if (s === 86400) return "24H";
            return s + "S";
        },
        
        formatLogTime(ts) {
            const d = new Date(ts + 'Z'); 
            return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute:'2-digit', second:'2-digit' }) + ' ' + d.toLocaleDateString('en-US', { month:'short', day:'numeric' });
        },
        
        async checkAuth() {
            this.jwtToken = sessionStorage.getItem('noc_jwt') || '';
            if (this.jwtToken) {
                try {
                    const res = await fetch('/api/live', { headers: { 'Authorization': `Bearer ${this.jwtToken}` } });
                    if (res.ok) {
                        this.isAuthenticated = true;
                        this.finalizeLoad();
                        return;
                    } else if (res.status === 401) {
                        this.lockConsole();
                    }
                } catch(e) {}
            }
            this.isLoading = false;
        },
        
        async login() {
            if (!this.password) return;
            try {
                const res = await fetch('/api/admin/login', { 
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password: this.password })
                });
                if (res.ok) {
                    const json = await res.json();
                    this.isAuthenticated = true;
                    this.jwtToken = json.token;
                    sessionStorage.setItem('noc_jwt', this.jwtToken);
                    this.password = '';
                    this.finalizeLoad();
                } else {
                    alert("Authentication Failed. Invalid Master Password.");
                    this.isLoading = false;
                }
            } catch (e) { 
                alert("Connection failed."); 
                this.isLoading = false;
            }
        },
        
        lockConsole() {
            this.isAuthenticated = false;
            this.jwtToken = '';
            this.password = '';
            sessionStorage.removeItem('noc_jwt');
        },
        
        finalizeLoad() {
            const minLoadTime = new Promise(resolve => setTimeout(resolve, 1000));
            const fetch1 = this.fetchData();
            const fetch2 = this.fetchLogs();
            const fetch3 = this.loadIntegrations();
            
            Promise.all([minLoadTime, fetch1, fetch2, fetch3]).then(() => {
                this.isLoading = false;
            });
        },
        
        async fetchData() {
            const res = await fetch('/api/live', { headers: { 'Authorization': `Bearer ${this.jwtToken}` } });
            if (res.status === 401) { this.lockConsole(); return; }
            const json = await res.json();
            if (json.status === 'success') {
                this.targets = json.data;
                this.settings = json.settings || { global_mute: '0', hud_priorities: '' };
                this.hudPriorities = this.settings.hud_priorities ? this.settings.hud_priorities.split(',') : [];
            }
        },
        
        async fetchLogs() {
            if (!this.isAuthenticated) return;
            const res = await fetch('/api/admin/events', { headers: { 'Authorization': `Bearer ${this.jwtToken}` }});
            if (res.status === 401) { this.lockConsole(); return; }
            const json = await res.json();
            if (json.status === 'success') this.logs = json.data;
        },
        
        async loadIntegrations() {
            try {
                const res = await fetch('/api/admin/config', { headers: { 'Authorization': `Bearer ${this.jwtToken}` }});
                if (res.status === 401) { this.lockConsole(); return; }
                const json = await res.json();
                if (json.status === 'success') {
                    this.integrations = json.data;
                }
            } catch(e) { alert("Failed to fetch configurations."); }
        },
        
        async _api(endpoint, payload, method='POST') {
            const res = await fetch(endpoint, {
                method: method,
                headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${this.jwtToken}` },
                body: JSON.stringify(payload)
            });
            if (res.status === 401) {
                this.lockConsole();
                return false;
            }
            if (!res.ok) {
                const json = await res.json();
                alert("System Error: " + (json.detail || "Request Failed"));
                return false;
            }
            return true;
        },
        
        togglePriority(id) {
            if (this.hudPriorities.includes(id)) {
                this.hudPriorities = this.hudPriorities.filter(p => p !== id);
            } else if (this.hudPriorities.length < 2) {
                this.hudPriorities.push(id);
            }
        },

        async saveHudPriorities() {
            this.saveIntegrations(true);
        },

        async saveIntegrations(silent = false) {
            this.isSavingIntegrations = true;
            
            const wanTarget = this.targets.find(t => t.uid === 'mnl_wan_primary');
            
            const payload = {
                wan_target: wanTarget ? wanTarget.target_address : '1.1.1.1',
                wan_name: wanTarget ? wanTarget.entity_name : 'WAN Gateway',
                hud_priorities: this.hudPriorities,
                plugins: {
                    proxmox_ip: this.integrations.plugin_proxmox_ip,
                    proxmox_token_id: this.integrations.plugin_proxmox_token_id,
                    proxmox_token_secret: this.integrations.plugin_proxmox_token_secret,
                    esxi_ip: this.integrations.plugin_esxi_ip,
                    esxi_user: this.integrations.plugin_esxi_user,
                    esxi_password: this.integrations.plugin_esxi_password,
                    truenas_ip: this.integrations.plugin_truenas_ip,
                    truenas_token: this.integrations.plugin_truenas_token,
                    synology_ip: this.integrations.plugin_synology_ip,
                    synology_user: this.integrations.plugin_synology_user,
                    synology_password: this.integrations.plugin_synology_password,
                    qnap_ip: this.integrations.plugin_qnap_ip,
                    qnap_user: this.integrations.plugin_qnap_user,
                    qnap_password: this.integrations.plugin_qnap_password,
                    portainer_url: this.integrations.plugin_portainer_url,
                    portainer_token: this.integrations.plugin_portainer_token,
                    pterodactyl_url: this.integrations.plugin_pterodactyl_url,
                    pterodactyl_token: this.integrations.plugin_pterodactyl_token,
                    pihole_ip: this.integrations.plugin_pihole_ip,
                    pihole_app_password: this.integrations.plugin_pihole_app_password,
                    jellyfin_url: this.integrations.plugin_jellyfin_url,
                    jellyfin_token: this.integrations.plugin_jellyfin_token,
                    plex_url: this.integrations.plugin_plex_url,
                    plex_token: this.integrations.plugin_plex_token
                },
                notifications: {
                    telegram_token: this.integrations.notify_telegram_token,
                    telegram_chat_id: this.integrations.notify_telegram_chat_id,
                    discord_webhook_url: this.integrations.notify_discord_webhook_url
                },
                discovered_targets: [] 
            };

            const success = await this._api('/api/setup/finalize', payload);
            if (success) {
                if (!silent) alert("Configurations Updated Successfully.");
                setTimeout(() => this.fetchData(), 1000);
            }
            this.isSavingIntegrations = false;
            return success;
        },

        async scanDevices() {
            const saved = await this.saveIntegrations(true);
            if (!saved) {
                alert("Failed to securely save credentials. Scan aborted.");
                return;
            }
            
            this.isScanning = true;
            try {
                const res = await fetch('/api/admin/scan-devices', {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${this.jwtToken}` }
                });
                const data = await res.json();
                if (data.status === 'success') {
                    let msg = `Topology Scan Complete.\nAdded ${data.added_count} new sensors.`;
                    if (data.errors && data.errors.length > 0) {
                        msg += `\n\nErrors encountered:\n- ` + data.errors.join('\n- ');
                    }
                    alert(msg);
                    this.fetchData();
                } else {
                    alert("Scan failed: " + data.detail);
                }
            } catch (e) {
                alert("Network error occurred during scan.");
            }
            this.isScanning = false;
        },

        async forcePoll() {
            if (await this._api('/api/admin/force-poll', {})) {
                alert("NETWORK PING COMMAND ISSUED.");
                setTimeout(() => { this.fetchData(); this.fetchLogs(); }, 2000);
            }
        },
        
        async triggerReport() {
            if (await this._api('/api/admin/trigger-report', {})) {
                alert("STATUS REPORT DISPATCHED TO MESSAGING CHANNELS.");
                setTimeout(() => { this.fetchLogs(); }, 2000);
            }
        },
        
        async toggleMute() {
            const newState = this.settings.global_mute === '1' ? 0 : 1;
            if (await this._api('/api/admin/sys-settings', { global_mute: newState })) this.fetchData();
        },
        
        async toggleTarget(t) {
            if (await this._api('/api/admin/toggle', { uid: t.uid })) {
                this.fetchData();
                setTimeout(() => this.fetchLogs(), 1500); 
            }
        },
        
        async deleteTarget(t) {
            if (!confirm(`WIPE ${t.entity_name} FROM DATABASE?`)) return;
            if (await this._api('/api/admin/targets', { uid: t.uid }, 'DELETE')) this.fetchData();
        },
        
        openNewSensorForm() {
            this.isEditing = false;
            this.form = { uid: '', name: '', entity_type: '', target_address: '', port: '', cooldown_interval: 3600, cpu_threshold: 90.0, mem_threshold: 90.0, location: 'Local', parent_uid: '', stack_group: '' };
            this.isSensorModalOpen = true;
        },
        
        editTarget(t) {
            this.isEditing = true;
            this.form = { 
                uid: t.uid,
                name: t.entity_name, 
                entity_type: t.entity_type, 
                target_address: t.target_address || '', 
                port: t.port || '', 
                cooldown_interval: t.cooldown_interval || 3600,
                cpu_threshold: t.cpu_threshold || 90.0,
                mem_threshold: t.mem_threshold || 90.0,
                location: t.location || 'Local',
                parent_uid: t.parent_uid || '',
                stack_group: t.stack_group || ''
            };
            this.isSensorModalOpen = true;
        },
        
        async saveTarget() {
            if (!this.form.port) this.form.port = 0;
            this.form.cooldown_interval = parseInt(this.form.cooldown_interval);
            this.form.cpu_threshold = parseFloat(this.form.cpu_threshold || 90.0);
            this.form.mem_threshold = parseFloat(this.form.mem_threshold || 90.0);
            
            let success = false;
            if (this.isEditing) success = await this._api('/api/admin/targets', this.form, 'PUT');
            else success = await this._api('/api/admin/targets', this.form, 'POST');
            
            if (success) { 
                this.isSensorModalOpen = false; 
                this.fetchData(); 
            }
        }
    }
}

