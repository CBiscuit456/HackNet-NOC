function nocApp() {
    return {
        isLoading: true,
        isDisplayMode: false,
        targets: [],
        sparklines: {},
        pihole: {},
        piholeLoaded: false,
        piholeActive: false,
        sysTime: '--:--:--',
        isMuted: false,
        hudPriorities: [],
        
        hostName: 'NO HOST',
        hostStatus: 'pending',
        hostCpu: 0,
        hostMemUsed: 0,
        hostMemTotal: 1,
        hostDiskUsed: 0,
        hostDiskTotal: 1,
        hostCpuThresh: 90.0,
        hostMemThresh: 90.0,
        hostRx: 0,
        hostTx: 0,
        hostUptime: 0,

        wanName: 'WAN ROUTING',
        wanPing: 0,
        wanLoss: 0.0,
        wanJitter: 0.0,
        wanStatus: 'pending',
        
        storageAgg: { used: 0, total: 0, percent: 0 },
        gameAgg: { online: 0, total: 0 },
        dockerAgg: { online: 0, total: 0 },
        mediaAgg: { online: 0, total: 0 },

        chartObj: null,

        activeView: 'overview',
        mobileMenuOpen: false,

        get viewTitle() {
            const map = {
                'overview': 'Fleet Overview',
                'compute': 'Compute & VMs',
                'storage': 'Storage Arrays',
                'apps': 'Apps & Network',
                'games': 'Game Fleet',
                'dormant': 'Dormant Infrastructure'
            };
            return map[this.activeView];
        },

        initApp() {
            const updateTime = () => { this.sysTime = new Date().toLocaleTimeString('en-US', { hour12: false }); };
            updateTime(); 
            setInterval(updateTime, 1000);

            const ctx = document.getElementById('wanChart').getContext('2d');
            this.chartObj = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: Array(60).fill(''),
                    datasets: [{
                        data: Array(60).fill(0),
                        borderColor: '#39ff14', 
                        backgroundColor: 'rgba(57, 255, 20, 0.15)',
                        borderWidth: 2, fill: true, tension: 0.3, pointRadius: 0
                    }]
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: { legend: { display: false }, tooltip: { enabled: false } },
                    scales: { x: { display: false }, y: { display: false, min: 0, suggestedMax: 50 } },
                    animation: { duration: 0 }
                }
            });

            const minLoadTime = new Promise(resolve => setTimeout(resolve, 1500));
            const dataLoad = this.fetchData();
            
            // Recursive polling loop to prevent API pile-ups
            const pollData = async () => {
                await this.fetchData();
                setTimeout(pollData, 10000);
            };
            
            Promise.all([minLoadTime, dataLoad]).then(() => {
                this.isLoading = false;
                setTimeout(pollData, 10000);
            });
        },
        
        toggleDisplayMode() {
            if (!document.fullscreenElement) {
                document.documentElement.requestFullscreen().catch(e => {
                    console.warn("Fullscreen request failed", e);
                });
            } else {
                if (document.exitFullscreen) {
                    document.exitFullscreen();
                }
            }
        },
        
        scrollToGrid() {
            this.$nextTick(() => {
                // ONLY scroll if we are on a desktop display (768px or wider)
                // This prevents the jarring screen-jump UX on mobile devices.
                if (window.innerWidth >= 768) {
                    const el = document.getElementById('grid-top');
                    if (el) {
                        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
                    }
                }
            });
        },

        hasPriority(key) {
            return this.hudPriorities.includes(key);
        },

        async fetchData() {
            try {
                const jwtToken = sessionStorage.getItem('noc_jwt') || '';
                const headers = jwtToken ? { 'Authorization': `Bearer ${jwtToken}` } : {};
                
                const res = await fetch('/api/live', { headers });
                
                if (res.status === 401) {
                    sessionStorage.removeItem('noc_jwt');
                    return this.fetchData();
                }
                
                const json = await res.json();
                
                if (json.status === 'success') {
                    if (json.is_setup_complete === false) {
                        window.location.href = '/setup';
                        return;
                    }
                    this.targets = json.data;
                    this.sparklines = json.sparklines || {};
                    this.pihole = json.pihole || {};
                    
                    const settings = json.settings || {};
                    this.isMuted = (settings.global_mute === '1');
                    this.hudPriorities = (settings.hud_priorities || '').split(',');
                    
                    this.piholeLoaded = Object.keys(this.pihole).length > 0;
                    const ps = (this.pihole.status || '').toLowerCase();
                    this.piholeActive = ['enabled', 'active', 'blocking'].includes(ps) || (this.pihole.queries_today > 0);

                    // Parse Host Data (Proxmox or ESXi)
                    const hosts = this.targets.filter(t => t.entity_type === 'host');
                    const host = hosts.length > 0 ? (hosts.find(t => t.status === 'running') || hosts[0]) : null;
                    
                    if (host) {
                        this.hostName = host.entity_name;
                        this.hostStatus = host.status || 'pending';
                        this.hostCpu = host.cpu_usage || 0;
                        this.hostMemUsed = host.mem_used || 0;
                        this.hostMemTotal = host.mem_total || 1;
                        this.hostDiskUsed = host.disk_used || 0;
                        this.hostDiskTotal = host.disk_total || 1;
                        this.hostCpuThresh = host.cpu_threshold || 90.0;
                        this.hostMemThresh = host.mem_threshold || 90.0;
                        this.hostRx = host.net_in || 0; 
                        this.hostTx = host.net_out || 0; 
                        this.hostUptime = parseInt(settings.host_uptime || 0);
                    } else {
                        this.hostName = 'NO HOST';
                        this.hostStatus = 'pending';
                    }

                    // Parse Primary WAN
                    const wans = this.targets.filter(t => t.entity_type === 'network' && t.location === 'WAN');
                    const wan = wans.length > 0 ? (wans.find(t => t.status === 'running') || wans[0]) : null;
                    
                    if (wan) {
                        this.wanName = wan.entity_name;
                        this.wanPing = (wan.ping_ms || 0).toFixed(0);
                        this.wanStatus = wan.status || 'pending';
                        this.wanLoss = (wan.packet_loss || 0.0).toFixed(1);
                        this.wanJitter = (wan.jitter_ms || 0.0).toFixed(1);
                    } else {
                        this.wanName = 'WAN ROUTING';
                        this.wanPing = 0;
                        this.wanStatus = 'pending';
                        this.wanLoss = 0.0;
                        this.wanJitter = 0.0;
                    }

                    // Dynamic Aggregations for HUD Panels
                    let sUsed = 0, sTot = 0, gOn = 0, gTot = 0, dOn = 0, dTot = 0, mOn = 0, mTot = 0;
                    
                    this.targets.forEach(t => {
                        if (t.is_enabled === 1) {
                            if (t.entity_type === 'storage') {
                                sUsed += (t.disk_used || 0);
                                sTot += (t.disk_total || 0);
                            }
                            if (t.entity_type === 'game') {
                                gTot++;
                                if (t.status === 'running') gOn++;
                            }
                            if (t.uid.startsWith('dkr_')) {
                                dTot++;
                                if (t.status === 'running') dOn++;
                            }
                            if (t.uid.startsWith('app_jellyfin') || t.uid.startsWith('app_plex')) {
                                mTot++;
                                if (t.status === 'running') mOn++;
                            }
                        }
                    });
                    
                    this.storageAgg = { used: sUsed, total: sTot, percent: sTot > 0 ? (sUsed / sTot) * 100 : 0 };
                    this.gameAgg = { online: gOn, total: gTot };
                    this.dockerAgg = { online: dOn, total: dTot };
                    this.mediaAgg = { online: mOn, total: mTot };
                    
                    // WAN Chart Updates
                    if (json.wan_history && json.wan_history.length > 0) {
                        const padded = [...Array(Math.max(0, 60 - json.wan_history.length)).fill(0), ...json.wan_history];
                        this.chartObj.data.datasets[0].data = padded;
                        this.chartObj.data.datasets[0].borderColor = this.wanStatus === 'running' ? '#39ff14' : '#ef4444';
                        this.chartObj.data.datasets[0].backgroundColor = this.wanStatus === 'running' ? 'rgba(57, 255, 20, 0.15)' : 'rgba(239, 68, 68, 0.15)';
                        this.chartObj.update();
                    }
                }
            } catch (e) {
                console.warn("NOC Background Poll Failed: ", e);
            }
        },

        isVerifying(t) {
            if (!t.down_since) return false;
            const downFor = (Date.now() / 1000) - t.down_since;
            return downFor < 30; // 30 second anti-panic grace period (~3 polls)
        },

        isActiveAlert(t) {
            if (t.is_enabled === 0) return false;
            if (t.entity_type === 'host') return false;
            if (t.entity_type === 'network' && t.location === 'WAN') return false;
            
            // RCA Logic for UI
            if (t.parent_uid) {
                const parent = this.targets.find(p => p.uid === t.parent_uid);
                if (parent && parent.status === 'stopped') return false;
            }

            if (t.status === 'stopped') return true;
            
            const cpuThresh = t.cpu_threshold || 90.0;
            const memThresh = t.mem_threshold || 90.0;
            
            if (['vm', 'lxc', 'game'].includes(t.entity_type)) {
                if ((t.cpu_usage || 0) >= cpuThresh) return true;
                const ramPct = ((t.mem_used || 0) / Math.max(t.mem_total || 1, 1)) * 100;
                if (ramPct >= memThresh) return true;
            }
            
            if (t.entity_type === 'storage') {
                const diskPct = ((t.disk_used || 0) / Math.max(t.disk_total || 1, 1)) * 100;
                if (diskPct >= memThresh) return true;
            }
            
            return false;
        },

        get fleetStatus() {
            const displayedTargets = this.targets.filter(t => {
                if (t.is_enabled === 0) return false;
                if (t.entity_type === 'host') { return t.entity_name === this.hostName; }
                if (t.entity_type === 'network' && t.location === 'WAN') { return t.entity_name === this.wanName; }
                return true;
            });

            const totalActive = displayedTargets.length;
            const online = displayedTargets.filter(t => t.status === 'running').length;
            const offline = displayedTargets.filter(t => t.status === 'stopped' && !this.isVerifying(t)).length;
            const paused = this.targets.filter(t => t.is_enabled === 0).length;
            const alerts = this.activeAlerts.length;
            
            return { totalActive, online, offline, paused, alerts };
        },

        get activeAlerts() {
            return this.targets.filter(t => this.isActiveAlert(t));
        },

        get activeTargets() {
            return this.targets.filter(t => 
                t.is_enabled === 1 && 
                t.entity_type !== 'host' && 
                !(t.entity_type === 'network' && t.location === 'WAN') &&
                !this.isActiveAlert(t)
            );
        },

        get pausedTargets() {
            return this.targets.filter(t => t.is_enabled === 0);
        },

        get filteredTargets() {
            const active = this.activeTargets;
            if (this.activeView === 'overview') return active;
            
            return active.filter(t => {
                if (this.activeView === 'compute') return ['vm', 'lxc'].includes(t.entity_type);
                if (this.activeView === 'storage') return t.entity_type === 'storage';
                if (this.activeView === 'apps') return ['app', 'http', 'network'].includes(t.entity_type);
                if (this.activeView === 'games') return t.entity_type === 'game';
                return false;
            });
        },

        getSparklinePoints(t) {
            const key = t.uid;
            const dataObj = this.sparklines[key];
            if (!dataObj) return '';
            const data = (t.entity_type === 'network' || t.entity_type === 'http') ? dataObj.ping : dataObj.cpu;
            if (!data || data.length === 0) return '';
            
            const max = Math.max(...data, 10);
            return data.map((val, i) => {
                const x = (i / Math.max(data.length - 1, 1)) * 100;
                const y = 30 - ((val / max) * 28);
                return `${x.toFixed(1)},${y.toFixed(1)}`;
            }).join(' ');
        },

        formatStorage(gb) {
            if (!gb) return '0G';
            if (gb >= 1000) return (gb / 1024).toFixed(2) + 'T';
            return gb.toFixed(1) + 'G';
        },
        
        formatSpeed(mbps) {
            const val = parseFloat(mbps) || 0;
            if (val >= 1.0) return val.toFixed(1) + ' MBPS';
            if (val > 0) return (val * 1024).toFixed(0) + ' KBPS';
            return '0 KBPS';
        },

        formatUptime(seconds) {
            if (!seconds || seconds == 0) return '0h 0m';
            const d = Math.floor(seconds / 86400);
            const h = Math.floor((seconds % 86400) / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            if (d > 0) return `${d}d ${h}h`;
            return `${h}h ${m}m`;
        },

        getCpuColor(val, threshold = 90.0) {
            if (val >= threshold) return '#ef4444'; 
            if (val >= (threshold * 0.8)) return '#f59e0b'; 
            return '#39ff14'; 
        },
        
        getRamColor(val, threshold = 90.0) {
            if (val >= threshold) return '#ef4444'; 
            if (val >= (threshold * 0.8)) return '#f59e0b'; 
            return '#39ff14'; 
        },

        getDialGradient(percent, color) {
            const p = Math.max(0, Math.min(percent || 0, 100));
            return `background: conic-gradient(${color} ${p}%, rgba(0, 0, 0, 1) ${p}%);`;
        },

        formatTime(timestamp) {
            if (!timestamp) return 'Unknown';
            const diff = Math.floor(Date.now() / 1000) - timestamp;
            const h = Math.floor(diff / 3600);
            const m = Math.floor((diff % 3600) / 60);
            if (h > 24) return Math.floor(h/24) + 'd ' + (h%24) + 'h';
            return (h > 0 ? h + 'h ' : '') + m + 'm';
        }
    }
}









