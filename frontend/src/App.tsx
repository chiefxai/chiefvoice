import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import axios from 'axios'
import {
  Alert, Avatar, Button, Card, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, Drawer, FormControl, IconButton, InputAdornment,
  InputLabel, MenuItem, Select, Snackbar, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Tooltip, Typography,
} from '@mui/material'
import HubRoundedIcon from '@mui/icons-material/HubRounded'
import KeyRoundedIcon from '@mui/icons-material/KeyRounded'
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import ContentCopyRoundedIcon from '@mui/icons-material/ContentCopyRounded'
import MenuRoundedIcon from '@mui/icons-material/MenuRounded'
import DarkModeRoundedIcon from '@mui/icons-material/DarkModeRounded'
import LightModeRoundedIcon from '@mui/icons-material/LightModeRounded'
import CheckCircleRoundedIcon from '@mui/icons-material/CheckCircleRounded'
import ErrorOutlineRoundedIcon from '@mui/icons-material/ErrorOutlineRounded'
import LoginPage from './LoginPage'
import { clearLoginCallbackFromUrl, currentUser, finishLogin, login, logout } from './auth'

type Page = 'Numbers' | 'API'
type RegisteredNumber = {
  provider: string
  phone_number: string
  customer_id: string
  customer_name: string
  inbound_enabled: boolean
  outbound_enabled: boolean
  agent_id: string | null
}
type PlatformApiKey = {
  id: string
  name: string
  key_prefix: string
  scopes: string[]
  created_at: string
  expires_at: string | null
  last_used_at: string | null
  revoked_at: string | null
  customer_id: string
  customer_name: string
  assigned_provider: string | null
  assigned_phone_number: string | null
}
type IssuedApiKey = { key: string; api_key_record: PlatformApiKey }

const API_BASE = (import.meta.env.VITE_API_URL || '').replace(/\/$/, '')
const http = axios.create({ baseURL: API_BASE, timeout: 30000 })
http.interceptors.request.use(config => {
  const token = sessionStorage.getItem('chiefvoice.token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

const api = {
  phoneNumbers: () => http.get<{ items: RegisteredNumber[] }>('/v1/admin/customers/phone-numbers').then(r => r.data.items),
  apiKeys: () => http.get<{ items: PlatformApiKey[] }>('/v1/admin/customers/api-keys').then(r => r.data.items),
  createApiKey: (number: RegisteredNumber) => http.post<IssuedApiKey>('/v1/admin/customers/api-keys', { provider: number.provider, phone_number: number.phone_number }).then(r => r.data),
  revokeApiKey: (id: string) => http.delete(`/v1/admin/customers/api-keys/${id}`),
}

const errorText = (error: unknown) => axios.isAxiosError(error)
  ? String(error.response?.data?.detail || error.message)
  : 'Something went wrong. Please try again.'

export default function App({ mode, onToggleTheme }: { mode: 'light' | 'dark'; onToggleTheme: () => void }) {
  const queryClient = useQueryClient()
  const [page, setPage] = useState<Page>('Numbers')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [token, setToken] = useState(sessionStorage.getItem('chiefvoice.token') || '')
  const [authReady, setAuthReady] = useState(false)
  const [signingIn, setSigningIn] = useState(false)
  const [authError, setAuthError] = useState('')
  const [assignmentOpen, setAssignmentOpen] = useState(false)
  const [selectedNumber, setSelectedNumber] = useState('')
  const [oneTimeKey, setOneTimeKey] = useState<IssuedApiKey | null>(null)
  const [toast, setToast] = useState('')
  const connected = Boolean(token)

  useEffect(() => {
    let active = true
    const initializeAuth = async () => {
      const params = new URLSearchParams(window.location.search)
      const isCallback = params.has('code') || params.has('error')
      try {
        const user = isCallback
          ? await finishLogin()
          : await currentUser()
        if (isCallback) clearLoginCallbackFromUrl()
        if (!user || user.expired || !user.access_token) {
          sessionStorage.removeItem('chiefvoice.token')
          if (active) setToken('')
          return
        }
        sessionStorage.setItem('chiefvoice.token', user.access_token)
        if (active) setToken(user.access_token)
      } catch (error) {
        if (isCallback) clearLoginCallbackFromUrl()
        sessionStorage.removeItem('chiefvoice.token')
        if (active) {
          setToken('')
          setAuthError(error instanceof Error ? error.message : 'OIDC sign-in failed. Please try again.')
        }
      } finally {
        if (active) setAuthReady(true)
      }
    }
    void initializeAuth()
    return () => { active = false }
  }, [])
  const numbersQuery = useQuery({ queryKey: ['platform-phone-numbers'], queryFn: api.phoneNumbers, enabled: connected, retry: false })
  const keysQuery = useQuery({ queryKey: ['platform-api-keys'], queryFn: api.apiKeys, enabled: connected && page === 'API', retry: false })
  const issueMutation = useMutation({
    mutationFn: (number: RegisteredNumber) => api.createApiKey(number),
    onSuccess: result => {
      void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] })
      setOneTimeKey(result)
      setAssignmentOpen(false)
      setSelectedNumber('')
      setToast('API key created for the selected phone number.')
    },
    onError: error => setToast(errorText(error)),
  })
  const revokeMutation = useMutation({
    mutationFn: api.revokeApiKey,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] })
      setToast('API key revoked.')
    },
    onError: error => setToast(errorText(error)),
  })
  const currentNumbers = numbersQuery.data || []
  const outboundNumbers = currentNumbers.filter(number => number.outbound_enabled)
  const selected = outboundNumbers.find(number => `${number.provider}|${number.phone_number}` === selectedNumber)
  const connect = async () => {
    setSigningIn(true)
    setAuthError('')
    try {
      await login()
    } catch (error) {
      setSigningIn(false)
      setAuthError(error instanceof Error ? error.message : 'Could not start OIDC sign-in.')
    }
  }
  const disconnect = async () => {
    await logout()
    sessionStorage.removeItem('chiefvoice.token')
    setToken('')
    setAuthError('')
    void queryClient.clear()
  }
  const navigation = <><div className="brand"><div className="brand-mark"><HubRoundedIcon fontSize="small" /></div><div className="brand-word">chief<span>voice</span></div></div><div className="nav-caption">Platform</div><div className="nav-list">{(['Numbers', 'API'] as const).map(item => <Button key={item} className={`nav-item ${page === item ? 'active' : ''}`} onClick={() => { setPage(item); setDrawerOpen(false) }} startIcon={item === 'Numbers' ? <HubRoundedIcon /> : <KeyRoundedIcon />}>{item}</Button>)}</div></>

  if (!authReady || !connected) {
    return <LoginPage loading={!authReady} signingIn={signingIn} error={authError} onSignIn={() => { void connect() }} />
  }

  return <div className={`app-shell ${mode === 'dark' ? 'theme-dark' : ''}`}>
    <aside className="sidebar">{navigation}</aside>
    <Drawer open={drawerOpen} onClose={() => setDrawerOpen(false)} PaperProps={{ sx: { width: 248, bgcolor: '#101b2d', color: 'white', p: 2 } }}><div className="sidebar open" style={{ position: 'static', width: 'auto', minHeight: '100%', padding: 0 }}>{navigation}</div></Drawer>
    <main className="main-area">
      <header className="topbar">
        <Stack direction="row" alignItems="center" gap={1}><IconButton className="mobile-menu" onClick={() => setDrawerOpen(true)}><MenuRoundedIcon /></IconButton><div className="eyebrow">Platform console</div></Stack>
        <div className="auth-bar">
          <Chip size="small" icon={connected ? <CheckCircleRoundedIcon /> : <ErrorOutlineRoundedIcon />} label={connected ? 'Signed in' : 'Not connected'} color={connected ? 'success' : 'default'} variant="outlined" sx={{ fontSize: 11, height: 27 }} />
          <Tooltip title={`Switch to ${mode === 'light' ? 'dark' : 'light'} theme`}><IconButton size="small" onClick={onToggleTheme} aria-label="Toggle color theme">{mode === 'light' ? <DarkModeRoundedIcon fontSize="small" /> : <LightModeRoundedIcon fontSize="small" />}</IconButton></Tooltip>
          <Button size="small" onClick={() => { void disconnect() }} color="inherit">Sign out</Button>
          <Avatar sx={{ width: 31, height: 31, bgcolor: '#e7eaff', color: '#465bd2', fontSize: 12, fontWeight: 800 }}>CV</Avatar>
        </div>
      </header>
      <div className="content">
        {page === 'Numbers' && <NumbersPage connected={connected} onConnect={() => { void connect() }} numbers={currentNumbers} loading={numbersQuery.isLoading} error={numbersQuery.error} />}
        {page === 'API' && <ApiPage connected={connected} onConnect={() => { void connect() }} numbers={outboundNumbers} keys={keysQuery.data || []} loading={keysQuery.isLoading} error={keysQuery.error} onCreate={() => { setSelectedNumber(''); setAssignmentOpen(true) }} onRevoke={id => { if (window.confirm('Revoke this API key? Requests using it will stop working immediately.')) revokeMutation.mutate(id) }} />}
      </div>
    </main>

    <Dialog open={assignmentOpen} onClose={() => setAssignmentOpen(false)} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>Create API for a phone number</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="info" sx={{ fontSize: 12 }}>The new API key will be restricted to calls from this outbound-enabled phone number.</Alert>
        <FormControl fullWidth disabled={!outboundNumbers.length}>
          <InputLabel id="assigned-number-label">Phone number</InputLabel>
          <Select labelId="assigned-number-label" value={selectedNumber} label="Phone number" onChange={event => setSelectedNumber(String(event.target.value))}>
            {outboundNumbers.map(number => <MenuItem key={`${number.provider}|${number.phone_number}`} value={`${number.provider}|${number.phone_number}`}>{number.phone_number} · {number.provider} · {number.customer_name}</MenuItem>)}
          </Select>
        </FormControl>
        {!outboundNumbers.length && <Alert severity="warning">There are no outbound-enabled numbers yet. Add a provider number before creating an API key.</Alert>}
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setAssignmentOpen(false)}>Cancel</Button><Button variant="contained" disabled={!selected || issueMutation.isPending} onClick={() => selected && issueMutation.mutate(selected)}>{issueMutation.isPending ? 'Creating…' : 'Create API'}</Button></DialogActions>
    </Dialog>

    <Dialog open={!!oneTimeKey} onClose={() => setOneTimeKey(null)} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>Copy the API key</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="warning">The full API key appears once. Save it in the customer’s secret manager before closing this window.</Alert>
        <Typography sx={{ fontSize: 12 }}>Assigned phone: <b>{oneTimeKey?.api_key_record.assigned_phone_number}</b> · {oneTimeKey?.api_key_record.assigned_provider}</Typography>
        <TextField label="API key" value={oneTimeKey?.key || ''} fullWidth multiline minRows={2} InputProps={{ readOnly: true, endAdornment: <InputAdornment position="end"><Tooltip title="Copy key"><IconButton onClick={() => { if (oneTimeKey) void navigator.clipboard.writeText(oneTimeKey.key) }}><ContentCopyRoundedIcon /></IconButton></Tooltip></InputAdornment> }} />
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button variant="contained" onClick={() => setOneTimeKey(null)}>Done</Button></DialogActions>
    </Dialog>

    <Snackbar open={Boolean(toast)} autoHideDuration={5000} onClose={() => setToast('')} anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}><Alert onClose={() => setToast('')} severity={toast.toLowerCase().includes('error') || toast.toLowerCase().includes('failed') ? 'error' : 'success'} variant="filled">{toast}</Alert></Snackbar>
  </div>
}

function NumbersPage({ connected, onConnect, numbers, loading, error }: { connected: boolean; onConnect: () => void; numbers: RegisteredNumber[]; loading: boolean; error: unknown }) {
  return <><PageIntro eyebrow="PHONE INVENTORY" title="Numbers" subtitle="Phone numbers registered across providers and customer accounts." />
    <Card className="surface-card" sx={{ mt: 3 }}>
      {!connected ? <ConnectPrompt onConnect={onConnect} /> : loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : numbers.length ? <Table size="small"><TableHead><TableRow>{['Phone number', 'Provider', 'Customer', 'Inbound', 'Outbound', 'Agent'].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{numbers.map(number => <TableRow key={`${number.provider}|${number.phone_number}|${number.customer_id}`} hover><TableCell className="table-cell" sx={{ fontWeight: 700 }}>{number.phone_number}</TableCell><TableCell className="table-cell"><Chip size="small" label={number.provider} variant="outlined" sx={{ textTransform: 'capitalize' }} /></TableCell><TableCell className="table-cell">{number.customer_name}</TableCell><TableCell className="table-cell"><StatusChip enabled={number.inbound_enabled} /></TableCell><TableCell className="table-cell"><StatusChip enabled={number.outbound_enabled} /></TableCell><TableCell className="table-cell">{number.agent_id || 'Default agent'}</TableCell></TableRow>)}</TableBody></Table> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><HubRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No phone numbers registered</b><div style={{ fontSize: 12, marginTop: 6 }}>Numbers appear here when customer provider integrations register them.</div></div>}
    </Card>
  </>
}

function ApiPage({ connected, onConnect, numbers, keys, loading, error, onCreate, onRevoke }: { connected: boolean; onConnect: () => void; numbers: RegisteredNumber[]; keys: PlatformApiKey[]; loading: boolean; error: unknown; onCreate: () => void; onRevoke: (id: string) => void }) {
  return <><PageIntro eyebrow="DEVELOPER ACCESS" title="API" subtitle="Create a phone-number API key and manage existing keys." action={<Button variant="contained" startIcon={<AddRoundedIcon />} onClick={onCreate} disabled={!connected || !numbers.length}>Create API</Button>} />
    <Card className="surface-card" sx={{ mt: 3 }}>
      {!connected ? <ConnectPrompt onConnect={onConnect} /> : loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : keys.length ? <Table size="small"><TableHead><TableRow>{['API key', 'Phone number', 'Provider', 'Customer', 'Created', 'Expires', 'State', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{keys.map(key => <TableRow key={key.id} hover><TableCell className="table-cell"><code>{key.key_prefix}</code></TableCell><TableCell className="table-cell" sx={{ fontWeight: 700 }}>{key.assigned_phone_number || 'Unassigned'}</TableCell><TableCell className="table-cell">{key.assigned_provider || '—'}</TableCell><TableCell className="table-cell">{key.customer_name}</TableCell><TableCell className="table-cell">{new Date(key.created_at).toLocaleDateString()}</TableCell><TableCell className="table-cell">{key.expires_at ? new Date(key.expires_at).toLocaleDateString() : 'Never'}</TableCell><TableCell className="table-cell"><StatusChip enabled={!key.revoked_at} label={key.revoked_at ? 'Revoked' : 'Active'} /></TableCell><TableCell className="table-cell">{!key.revoked_at && <Button color="error" size="small" onClick={() => onRevoke(key.id)}>Revoke</Button>}</TableCell></TableRow>)}</TableBody></Table> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><KeyRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No API keys created</b><div style={{ fontSize: 12, marginTop: 6 }}>{numbers.length ? 'Create an API key and assign it to an outbound-enabled phone number.' : 'Register an outbound-enabled phone number first.'}</div></div>}
    </Card>
  </>
}

function ConnectPrompt({ onConnect }: { onConnect: () => void }) { return <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><KeyRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>Sign in to manage the platform</b><div style={{ fontSize: 12, marginTop: 6 }}>Use your allowlisted platform administrator identity.</div><Button sx={{ mt: 1.5 }} variant="contained" onClick={onConnect}>Sign in with OIDC</Button></div> }
function StatusChip({ enabled, label }: { enabled: boolean; label?: string }) { return <Chip size="small" label={label || (enabled ? 'Enabled' : 'Disabled')} color={enabled ? 'success' : 'default'} variant={enabled ? 'filled' : 'outlined'} sx={{ fontSize: 10, height: 22 }} /> }
function PageIntro({ eyebrow, title, subtitle, action }: { eyebrow: string; title: string; subtitle: string; action?: React.ReactNode }) { return <div style={{ display: 'flex', alignItems: 'end', justifyContent: 'space-between', gap: 18, flexWrap: 'wrap' }}><div><div className="eyebrow">{eyebrow}</div><Typography className="page-title" variant="h4">{title}</Typography><div className="page-subtitle">{subtitle}</div></div>{action}</div> }
