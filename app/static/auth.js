// ===== Auth UI (user panel) =====
// Fetches /auth/me on load and provides toggle/render logic for the user panel.

let _currentUser = null;

// Fetch current user on page load
(async function initAuth() {
    try {
        const r = await fetch('/auth/me');
        if (r.ok) {
            _currentUser = await r.json();
        } else if (r.status === 401) {
            // Shouldn't happen (middleware redirects), but handle gracefully
            window.location.href = '/login';
        }
    } catch (e) {
        console.warn('Could not fetch /auth/me:', e);
    }
})();

function toggleUserPanel() {
    const panel = document.getElementById('userPanel');
    if (panel.classList.contains('show')) {
        closeUserPanel();
    } else {
        renderUserPanel();
        panel.classList.add('show');
    }
}

function closeUserPanel() {
    const panel = document.getElementById('userPanel');
    panel.classList.remove('show');
}

// Close the panel with Escape, matching the other modals.
document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    const panel = document.getElementById('userPanel');
    if (panel && panel.classList.contains('show')) closeUserPanel();
});

// Close panel when clicking the backdrop
document.getElementById('userPanel').addEventListener('click', function(e) {
    if (e.target === this) closeUserPanel();
});

function renderUserPanel() {
    const body = document.getElementById('userPanelBody');
    const user = _currentUser || { username: '…', role: 'user' };
    const isAdmin = user.role === 'admin';

    body.innerHTML = `
        <p style="margin-bottom:1rem;color:var(--text-muted,#888)">
            Signed in as <strong>${_esc(user.username)}</strong>
        </p>

        <!-- Change password -->
        <details style="margin-bottom:1rem;">
            <summary style="cursor:pointer;font-weight:600;">🔑 Change Password</summary>
            <div style="margin-top:.75rem;">
                <div class="form-group">
                    <label for="upCurrentPw">Current password</label>
                    <input type="password" id="upCurrentPw" class="form-control" placeholder="Current password">
                </div>
                <div class="form-group">
                    <label for="upNewPw">New password</label>
                    <input type="password" id="upNewPw" class="form-control" placeholder="New password">
                </div>
                <div class="form-group">
                    <label for="upConfirmPw">Confirm new password</label>
                    <input type="password" id="upConfirmPw" class="form-control" placeholder="Confirm new password">
                </div>
                <button class="btn btn-primary btn-sm" onclick="_changePw()">Save Password</button>
                <span id="upPwMsg" style="margin-left:.5rem;font-size:.875rem;"></span>
            </div>
        </details>

        <!-- Rename -->
        <details style="margin-bottom:1rem;">
            <summary style="cursor:pointer;font-weight:600;">✏️ Rename Account</summary>
            <div style="margin-top:.75rem;">
                <div class="form-group">
                    <label for="upNewName">New username</label>
                    <input type="text" id="upNewName" class="form-control" value="${_esc(user.username)}">
                </div>
                <button class="btn btn-primary btn-sm" onclick="_renameUser()">Save Username</button>
                <span id="upRenameMsg" style="margin-left:.5rem;font-size:.875rem;"></span>
            </div>
        </details>

        ${isAdmin ? _adminSection() : ''}

        <!-- Logout -->
        <div style="margin-top:1.25rem;border-top:1px solid var(--border-color,#333);padding-top:1rem;">
            <button class="btn btn-danger btn-sm" onclick="_logout()">🚪 Logout</button>
        </div>
    `;

    if (isAdmin) _loadAdminUsers();
}

function _adminSection() {
    return `
        <details id="adminSection" style="margin-bottom:1rem;">
            <summary style="cursor:pointer;font-weight:600;">🛠️ Manage Users</summary>
            <div style="margin-top:.75rem;">
                <div id="adminUserList" style="margin-bottom:1rem;">Loading…</div>
                <hr style="margin:.75rem 0;border-color:var(--border-color,#333);">
                <strong>Add User</strong>
                <div class="form-group" style="margin-top:.5rem;">
                    <label for="auUsername">Username</label>
                    <input type="text" id="auUsername" class="form-control" placeholder="username">
                </div>
                <div class="form-group">
                    <label for="auPassword">Password</label>
                    <input type="password" id="auPassword" class="form-control" placeholder="password">
                </div>
                <div class="form-group">
                    <label for="auRole">Role</label>
                    <select id="auRole" class="form-control">
                        <option value="user">user</option>
                        <option value="admin">admin</option>
                    </select>
                </div>
                <button class="btn btn-primary btn-sm" onclick="_addUser()">➕ Add User</button>
                <span id="auMsg" style="margin-left:.5rem;font-size:.875rem;"></span>
            </div>
        </details>
    `;
}

async function _loadAdminUsers() {
    const container = document.getElementById('adminUserList');
    if (!container) return;
    try {
        const r = await fetch('/auth/admin/users');
        if (!r.ok) { container.textContent = 'Error loading users'; return; }
        const users = await r.json();
        if (!users.length) { container.textContent = 'No users'; return; }
        container.innerHTML = users.map(u => `
            <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.4rem;">
                <span style="flex:1;">${_esc(u.username)} <small style="color:var(--text-muted,#888)">(${_esc(u.role)})</small></span>
                ${u.username !== (_currentUser && _currentUser.username)
                    ? `<button class="btn btn-sm btn-danger" data-username="${_esc(u.username)}" data-action="delete-user">🗑️</button>`
                    : '<span style="font-size:.75rem;color:var(--text-muted,#888)">(you)</span>'}
            </div>
        `).join('');
        // Attach delete handlers via data attributes (avoids inline onclick XSS)
        container.querySelectorAll('[data-action="delete-user"]').forEach(btn => {
            btn.addEventListener('click', () => _deleteUser(btn.dataset.username));
        });
    } catch (e) {
        container.textContent = 'Error: ' + e.message;
    }
}

async function _changePw() {
    const msg = document.getElementById('upPwMsg');
    const current = document.getElementById('upCurrentPw').value;
    const newPw = document.getElementById('upNewPw').value;
    const confirm = document.getElementById('upConfirmPw').value;
    msg.textContent = '';
    try {
        const r = await fetch('/auth/change-password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ current_password: current, new_password: newPw, confirm_password: confirm }),
        });
        const data = await r.json().catch(() => ({}));
        if (r.ok) {
            msg.style.color = 'var(--success-color,#4caf50)';
            msg.textContent = '✓ Password changed';
            document.getElementById('upCurrentPw').value = '';
            document.getElementById('upNewPw').value = '';
            document.getElementById('upConfirmPw').value = '';
        } else {
            msg.style.color = 'var(--error-color,#e94560)';
            msg.textContent = data.detail || 'Error';
        }
    } catch (e) {
        msg.style.color = 'var(--error-color,#e94560)';
        msg.textContent = 'Network error';
    }
}

async function _renameUser() {
    const msg = document.getElementById('upRenameMsg');
    const newName = document.getElementById('upNewName').value.trim();
    msg.textContent = '';
    try {
        const r = await fetch('/auth/rename', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ new_username: newName }),
        });
        const data = await r.json().catch(() => ({}));
        if (r.ok) {
            msg.style.color = 'var(--success-color,#4caf50)';
            msg.textContent = '✓ Renamed';
            if (_currentUser && data.username) _currentUser.username = data.username;
        } else {
            msg.style.color = 'var(--error-color,#e94560)';
            msg.textContent = data.detail || 'Error';
        }
    } catch (e) {
        msg.style.color = 'var(--error-color,#e94560)';
        msg.textContent = 'Network error';
    }
}

async function _addUser() {
    const msg = document.getElementById('auMsg');
    const username = document.getElementById('auUsername').value.trim();
    const password = document.getElementById('auPassword').value;
    const role = document.getElementById('auRole').value;
    msg.textContent = '';
    try {
        const r = await fetch('/auth/admin/users', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password, role }),
        });
        const data = await r.json().catch(() => ({}));
        if (r.ok) {
            msg.style.color = 'var(--success-color,#4caf50)';
            msg.textContent = '✓ User added';
            document.getElementById('auUsername').value = '';
            document.getElementById('auPassword').value = '';
            _loadAdminUsers();
        } else {
            msg.style.color = 'var(--error-color,#e94560)';
            msg.textContent = data.detail || 'Error';
        }
    } catch (e) {
        msg.style.color = 'var(--error-color,#e94560)';
        msg.textContent = 'Network error';
    }
}

async function _deleteUser(username) {
    if (!confirm(`Delete user "${username}"?`)) return;
    try {
        const r = await fetch(`/auth/admin/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
        if (r.ok) {
            _loadAdminUsers();
        } else {
            const data = await r.json().catch(() => ({}));
            alert(data.detail || 'Error deleting user');
        }
    } catch (e) {
        alert('Network error');
    }
}

async function _logout() {
    await fetch('/auth/logout', { method: 'POST' });
    window.location.href = '/login';
}

/** Minimal HTML-escape helper */
function _esc(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}
