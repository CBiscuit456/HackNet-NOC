function setupWizard() {
    return {
        step: 1, adminExists: false, jwtToken: '', newPassword: '', confirmPassword: '', loginPassword: '', authError: '', isSaving: false, isDiscovering: false,
        wanTarget: '1.1.1.1', wanName: 'Cloudflare DNS',
        
        // Redesigned HUD Categories mapping to triggers
        basePriorityOptions: [
            { id: 'compute', label: 'Compute Core', triggers: ['proxmox', 'esxi'] },
            { id: 'storage', label: 'Storage Fleet', triggers: ['truenas', 'synology', 'qnap'] },
            { id: 'docker', label: 'Docker Fleet', triggers: ['portainer'] },
            { id: 'dns', label: 'DNS Sinkhole', triggers: ['pihole'] },
            { id: 'games', label: 'Game Fleet', triggers: ['pterodactyl'] },
            { id: 'media', label: 'Media Services', triggers: ['jellyfin', 'plex'] }
        ],
        hudPriorities: [], 
        
        plugins: {
            proxmox: { enabled: false, testStatus: '', testMessage: '', label: 'Proxmox VE', desc: 'Hypervisor', data: { ip: '', token_id: '', token_secret: '' } },
            esxi: { enabled: false, testStatus: '', testMessage: '', label: 'VMware ESXi', desc: 'Hypervisor', data: { ip: '', user: '', password: '' } },
            truenas: { enabled: false, testStatus: '', testMessage: '', label: 'TrueNAS', desc: 'Storage Array', data: { ip: '', token: '' } },
            synology: { enabled: false, testStatus: '', testMessage: '', label: 'Synology', desc: 'Storage Array', data: { ip: '', user: '', password: '' } },
            qnap: { enabled: false, testStatus: '', testMessage: '', label: 'QNAP', desc: 'Storage Array', data: { ip: '', user: '', password: '' } },
            portainer: { enabled: false, testStatus: '', testMessage: '', label: 'Portainer', desc: 'Docker Manager', data: { url: '', token: '' } },
            pterodactyl: { enabled: false, testStatus: '', testMessage: '', label: 'Pterodactyl', desc: 'Game Panel', data: { url: '', token: '' } },
            pihole: { enabled: false, testStatus: '', testMessage: '', label: 'Pi-Hole', desc: 'DNS Sinkhole', data: { ip: '', app_password: '' } },
            jellyfin: { enabled: false, testStatus: '', testMessage: '', label: 'Jellyfin', desc: 'Media Server', data: { url: '', token: '' } },
            plex: { enabled: false, testStatus: '', testMessage: '', label: 'Plex', desc: 'Media Server', data: { url: '', token: '' } }
        },
        notifications: { telegram_token: '', telegram_chat_id: '', discord_webhook_url: '' },
        
        discoveredTargets: [],
        proposedStacks: [],
        discoveryErrors: [],
        
        preFlightSummary: { activePlugins: 0, totalSensors: 0 },

        async checkStatus() {
            try {
                const res = await fetch('/api/setup/status');
                const data = await res.json();
                this.adminExists = data.admin_exists;
                
                this.jwtToken = sessionStorage.getItem('noc_jwt') || '';
                
                if (data.is_setup_complete) { window.location.href = '/'; }
            } catch (e) { this.authError = "Backend offline or starting up..."; }
        },

        async createAdmin() {
            if (this.newPassword.length < 8) { this.authError = "PASSWORD MUST BE AT LEAST 8 CHARACTERS."; return; }
            if (this.newPassword !== this.confirmPassword) { this.authError = "PASSWORDS DO NOT MATCH."; return; }
            
            try {
                const res = await fetch('/api/setup/init', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password: this.newPassword })
                });
                if (res.ok) {
                    const data = await res.json();
                    this.adminExists = true;
                    this.jwtToken = data.token;
                    this.authError = "";
                    sessionStorage.setItem('noc_jwt', this.jwtToken);
                    this.step = 2;
                } else {
                    const err = await res.json();
                    this.authError = err.detail || "FAILED TO CREATE ACCOUNT.";
                }
            } catch (e) { this.authError = "NETWORK ERROR."; }
        },

        async validateAuth() {
            if (!this.loginPassword) { this.authError = "PASSWORD REQUIRED."; return; }
            this.authError = "VALIDATING...";
            try {
                const res = await fetch('/api/admin/login', { 
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password: this.loginPassword })
                });
                if (res.status === 401) { 
                    this.authError = "INVALID MASTER PASSWORD."; 
                } else if (res.ok) {
                    const data = await res.json();
                    this.jwtToken = data.token;
                    this.authError = ""; 
                    sessionStorage.setItem('noc_jwt', this.jwtToken); 
                    this.step = 2; 
                }
            } catch (e) { this.authError = "NETWORK ERROR."; }
        },

        togglePlugin(name) { this.plugins[name].enabled = !this.plugins[name].enabled; },
        hasAnyPlugin() { return Object.values(this.plugins).some(p => p.enabled); },
        
        async testConnection(plugin) {
            this.plugins[plugin].testStatus = 'testing';
            this.plugins[plugin].testMessage = 'Contacting Server...';
            try {
                const res = await fetch('/api/setup/test-connection', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${this.jwtToken}` },
                    body: JSON.stringify({ plugin: plugin, data: this.plugins[plugin].data })
                });
                const data = await res.json();
                if (data.status === 'success') {
                    this.plugins[plugin].testStatus = 'success';
                    this.plugins[plugin].testMessage = data.message;
                } else {
                    this.plugins[plugin].testStatus = 'error';
                    this.plugins[plugin].testMessage = data.detail || 'Connection Failed';
                }
            } catch (e) {
                this.plugins[plugin].testStatus = 'error';
                this.plugins[plugin].testMessage = "Backend Network Error";
            }
        },

        async runDiscovery() {
            this.isDiscovering = true;
            this.discoveryErrors = [];
            
            try {
                let pluginPayload = {};
                for (const [key, val] of Object.entries(this.plugins)) {
                    if (val.enabled) {
                        pluginPayload[key] = val.data; 
                    }
                }
                
                const res = await fetch('/api/setup/discover', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${this.jwtToken}` },
                    body: JSON.stringify({ plugins: pluginPayload })
                });
                
                const data = await res.json();
                if (data.status === 'success') {
                    this.discoveredTargets = data.discovered || [];
                    this.proposedStacks = data.proposed_stacks || [];
                    
                    if (data.errors && data.errors.length > 0) {
                        this.discoveryErrors = data.errors;
                    }
                    
                    this.proposedStacks.forEach(stack => {
                        let child = this.discoveredTargets.find(t => t.uid === stack.child.uid);
                        if (child) {
                            child.parent_uid = stack.parent.uid;
                            child.stack_group = stack.parent.name;
                        }
                    });
                    this.step = 4;
                }
            } catch (e) {
                this.discoveryErrors = ["Failed to reach the local discovery engine. Check your API logs."];
            }
            this.isDiscovering = false;
        },

        get availablePriorities() {
            return this.basePriorityOptions.filter(opt => {
                return opt.triggers.some(t => this.plugins[t] && this.plugins[t].enabled);
            });
        },

        prepareStep5() {
            // Auto-cleanup: Ensure user doesn't have priorities selected for disabled plugins
            this.hudPriorities = this.hudPriorities.filter(id => {
                const opt = this.basePriorityOptions.find(o => o.id === id);
                return opt && opt.triggers.some(t => this.plugins[t] && this.plugins[t].enabled);
            });
            
            // Smart UX: If they only selected 2 or fewer categories total, auto-select them all.
            const availableIds = this.availablePriorities.map(p => p.id);
            if (availableIds.length > 0 && availableIds.length <= 2 && this.hudPriorities.length === 0) {
                this.hudPriorities = [...availableIds];
            }
            
            this.preFlightSummary.activePlugins = Object.values(this.plugins).filter(p => p.enabled).length;
            this.preFlightSummary.totalSensors = this.discoveredTargets.length;
            
            this.step = 5;
        },

        togglePriority(id) {
            if (this.hudPriorities.includes(id)) {
                this.hudPriorities = this.hudPriorities.filter(p => p !== id);
            } else if (this.hudPriorities.length < 2) {
                this.hudPriorities.push(id);
            }
        },

        async finalizeSetup() {
            if (!this.wanTarget) { alert("Please provide a WAN Target IP."); this.step = 2; return; }
            this.isSaving = true;
            
            let pluginsData = {};
            for (const [name, plugin] of Object.entries(this.plugins)) {
                if (plugin.enabled) {
                    for (const [key, val] of Object.entries(plugin.data)) pluginsData[`${name}_${key}`] = val;
                }
            }

            try {
                const res = await fetch('/api/setup/finalize', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${this.jwtToken}` },
                    body: JSON.stringify({ 
                        wan_target: this.wanTarget, 
                        wan_name: this.wanName,
                        hud_priorities: this.hudPriorities,
                        plugins: pluginsData, 
                        notifications: this.notifications,
                        discovered_targets: this.discoveredTargets
                    })
                });
                
                if (res.ok) {
                    setTimeout(() => { window.location.href = '/'; }, 1000);
                } else {
                    const err = await res.json();
                    alert(err.detail || "Setup failed."); 
                    this.isSaving = false;
                }
            } catch (e) { alert("Network error. Could not connect to API."); this.isSaving = false; }
        }
    }
}

