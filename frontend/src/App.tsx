import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import axios from 'axios'
import {
  Alert, Avatar, Button, Card, Checkbox, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, Drawer, FormControl, IconButton, InputAdornment,
  InputLabel, ListItemText, MenuItem, Select, Snackbar, Stack, Table, TableBody, TableCell,
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
  api_key_id: string | null
  api_key_name: string | null
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
  assigned_numbers: { provider: string; phone_number: string }[]
  vertex_project_id?: string | null
  vertex_location?: string | null
  vertex_credentials_configured?: boolean
}
type IssuedApiKey = { key: string; api_key_record: PlatformApiKey }

const API_BASE = (import.meta.env.VITE_API_URL || '').replace(/\/$/, '')
const http = axios.create({ baseURL: API_BASE, timeout: 30000 })
http.interceptors.request.use(config => {
  const token = sessionStorage.getItem('chiefvoice.token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})
http.interceptors.response.use(response => response, error => {
  if (axios.isAxiosError(error) && error.response?.status === 401) {
    sessionStorage.removeItem('chiefvoice.token')
    void logout().catch(() => undefined)
    window.dispatchEvent(new Event('chiefvoice-auth-expired'))
  }
  return Promise.reject(error)
})

const api = {
  phoneNumbers: () => http.get<{ items: RegisteredNumber[] }>('/v1/admin/customers/phone-numbers').then(r => r.data.items),
  apiKeys: () => http.get<{ items: PlatformApiKey[] }>('/v1/admin/customers/api-keys').then(r => r.data.items),
  createApiKey: (numbers: RegisteredNumber[], credentials: string, location: string) => http.post<IssuedApiKey>('/v1/admin/customers/api-keys', { phone_numbers: numbers.map(number => ({ provider: number.provider, phone_number: number.phone_number })), gcp_credentials_json: credentials, vertex_location: location }).then(r => r.data),
  updateApiConfig: (id: string, numbers: RegisteredNumber[], credentials: string, location: string) => http.put(`/v1/admin/customers/api-keys/${id}/config`, { phone_numbers: numbers.map(number => ({ provider: number.provider, phone_number: number.phone_number })), gcp_credentials_json: credentials || null, vertex_location: location }),
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
  const [selectedNumbers, setSelectedNumbers] = useState<string[]>([])
  const [gcpCredentials, setGcpCredentials] = useState('')
  const [vertexLocation, setVertexLocation] = useState('us-central1')
  const [managementKey, setManagementKey] = useState<PlatformApiKey | null>(null)
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

  useEffect(() => {
    const handleUnauthorized = () => {
      setToken('')
      setAuthReady(true)
      setAuthError('Your sign-in could not be verified. Please sign in again.')
      void queryClient.clear()
    }
    window.addEventListener('chiefvoice-auth-expired', handleUnauthorized)
    return () => window.removeEventListener('chiefvoice-auth-expired', handleUnauthorized)
  }, [queryClient])
  const numbersQuery = useQuery({ queryKey: ['platform-phone-numbers'], queryFn: api.phoneNumbers, enabled: connected, retry: false })
  const keysQuery = useQuery({ queryKey: ['platform-api-keys'], queryFn: api.apiKeys, enabled: connected && page === 'API', retry: false })
  const issueMutation = useMutation({
    mutationFn: ({ numbers, credentials, location }: { numbers: RegisteredNumber[]; credentials: string; location: string }) => api.createApiKey(numbers, credentials, location),
    onSuccess: result => {
      void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] })
      setOneTimeKey(result)
      setAssignmentOpen(false)
      setSelectedNumbers([])
      setGcpCredentials('')
      setVertexLocation('us-central1')
      setToast('API key created for the selected phone number.')
    },
    onError: error => setToast(errorText(error)),
  })
  const revokeMutation = useMutation({
    mutationFn: api.revokeApiKey,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] })
      setToast('API key revoked.')
    },
    onError: error => setToast(errorText(error)),
  })
  const manageNumbersMutation = useMutation({
    mutationFn: ({ key, numbers, credentials, location }: { key: PlatformApiKey; numbers: RegisteredNumber[]; credentials: string; location: string }) => api.updateApiConfig(key.id, numbers, credentials, location),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] })
      setManagementKey(null)
      setSelectedNumbers([])
      setToast('API phone number assignments updated.')
    },
    onError: error => setToast(errorText(error)),
  })
  const currentNumbers = numbersQuery.data || []
  const outboundNumbers = currentNumbers
    .filter(number => number.outbound_enabled && !number.api_key_id)
    .filter((number, index, numbers) => numbers.findIndex(candidate => candidate.phone_number === number.phone_number) === index)
  const selected = outboundNumbers.filter(number => selectedNumbers.includes(`${number.provider}|${number.phone_number}`))
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
        {page === 'API' && <ApiPage connected={connected} onConnect={() => { void connect() }} numbers={outboundNumbers} keys={keysQuery.data || []} loading={keysQuery.isLoading} error={keysQuery.error} onCreate={() => { setSelectedNumbers([]); setGcpCredentials(''); setVertexLocation('us-central1'); setAssignmentOpen(true) }} onManage={key => { setManagementKey(key); setSelectedNumbers(key.assigned_numbers.map(number => `${number.provider}|${number.phone_number}`)); setGcpCredentials(''); setVertexLocation(key.vertex_location || 'us-central1') }} onRevoke={id => { if (window.confirm('Revoke this API key? Requests using it will stop working immediately.')) revokeMutation.mutate(id) }} />}
      </div>
    </main>

    <Dialog open={assignmentOpen} onClose={() => setAssignmentOpen(false)} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>Create API for phone numbers</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="info" sx={{ fontSize: 12 }}>Each API key has its own encrypted Google Cloud service account credentials and can own multiple phone numbers. A number can belong to only one API key.</Alert>
        <TextField label="Google Cloud service account JSON" value={gcpCredentials} onChange={event => setGcpCredentials(event.target.value)} multiline minRows={5} fullWidth placeholder={'Paste the downloaded service account JSON key'} />
        <TextField label="Vertex AI location" value={vertexLocation} onChange={event => setVertexLocation(event.target.value)} fullWidth placeholder="us-central1" />
        <FormControl fullWidth disabled={!outboundNumbers.length}>
          <InputLabel id="assigned-number-label">Phone numbers</InputLabel>
          <Select multiple labelId="assigned-number-label" value={selectedNumbers} label="Phone numbers" onChange={event => setSelectedNumbers(typeof event.target.value === 'string' ? event.target.value.split(',') : event.target.value)} renderValue={values => `${values.length} phone number${values.length === 1 ? '' : 's'} selected`}>
            {outboundNumbers.map(number => <MenuItem key={`${number.provider}|${number.phone_number}`} value={`${number.provider}|${number.phone_number}`} disabled={Boolean(selected.length && selected[0].customer_id !== number.customer_id)}><Checkbox checked={selectedNumbers.includes(`${number.provider}|${number.phone_number}`)} size="small" /><ListItemText primary={`${number.phone_number} · ${number.provider}`} secondary={number.customer_name} /></MenuItem>)}
          </Select>
        </FormControl>
        {!outboundNumbers.length && <Alert severity="warning">There are no unassigned outbound-enabled numbers. Register a number or manage an existing API assignment.</Alert>}
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setAssignmentOpen(false)}>Cancel</Button><Button variant="contained" disabled={!selected.length || !gcpCredentials.trim() || !vertexLocation.trim() || issueMutation.isPending} onClick={() => selected.length && issueMutation.mutate({ numbers: selected, credentials: gcpCredentials, location: vertexLocation })}>{issueMutation.isPending ? 'Creating…' : 'Create API'}</Button></DialogActions>
    </Dialog>

    <Dialog open={!!oneTimeKey} onClose={() => setOneTimeKey(null)} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>Copy the API key</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="warning">The full API key appears once. Save it in the customer’s secret manager before closing this window.</Alert>
        <Typography sx={{ fontSize: 12 }}>Assigned phone numbers: <b>{oneTimeKey?.api_key_record.assigned_numbers.map(number => `${number.phone_number} · ${number.provider}`).join(', ')}</b></Typography>
        <TextField label="API key" value={oneTimeKey?.key || ''} fullWidth multiline minRows={2} InputProps={{ readOnly: true, endAdornment: <InputAdornment position="end"><Tooltip title="Copy key"><IconButton onClick={() => { if (oneTimeKey) void navigator.clipboard.writeText(oneTimeKey.key) }}><ContentCopyRoundedIcon /></IconButton></Tooltip></InputAdornment> }} />
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button variant="contained" onClick={() => setOneTimeKey(null)}>Done</Button></DialogActions>
    </Dialog>

    <Dialog open={!!managementKey} onClose={() => setManagementKey(null)} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>Edit API configuration</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="info" sx={{ fontSize: 12 }}>Saved service account credentials are encrypted. Leave the credential field blank to keep the current key; enter new JSON to rotate it.</Alert>
        <TextField label="Replace Google Cloud service account JSON (optional)" value={gcpCredentials} onChange={event => setGcpCredentials(event.target.value)} multiline minRows={4} fullWidth placeholder={managementKey?.vertex_credentials_configured ? 'Credentials are saved. Leave blank to keep them.' : 'Paste service account JSON'} />
        <TextField label="Vertex AI location" value={vertexLocation} onChange={event => setVertexLocation(event.target.value)} fullWidth />
        <FormControl fullWidth disabled={!managementKey}>
          <InputLabel id="manage-number-label">Phone numbers</InputLabel>
          <Select multiple labelId="manage-number-label" value={selectedNumbers} label="Phone numbers" onChange={event => setSelectedNumbers(typeof event.target.value === 'string' ? event.target.value.split(',') : event.target.value)} renderValue={values => `${values.length} phone number${values.length === 1 ? '' : 's'} selected`}>
            {currentNumbers.filter(number => number.customer_id === managementKey?.customer_id && number.outbound_enabled && (!number.api_key_id || number.api_key_id === managementKey?.id)).filter((number, index, list) => list.findIndex(candidate => candidate.phone_number === number.phone_number) === index).map(number => {
              const value = `${number.provider}|${number.phone_number}`
              return <MenuItem key={value} value={value}><Checkbox checked={selectedNumbers.includes(value)} size="small" /><ListItemText primary={`${number.phone_number} · ${number.provider}`} secondary={number.customer_name} /></MenuItem>
            })}
          </Select>
        </FormControl>
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setManagementKey(null)}>Cancel</Button><Button variant="contained" disabled={!managementKey || !selectedNumbers.length || (!gcpCredentials.trim() && !managementKey.vertex_credentials_configured) || manageNumbersMutation.isPending} onClick={() => { if (!managementKey) return; const numbers = currentNumbers.filter(number => selectedNumbers.includes(`${number.provider}|${number.phone_number}`)); manageNumbersMutation.mutate({ key: managementKey, numbers, credentials: gcpCredentials, location: vertexLocation }) }}>{manageNumbersMutation.isPending ? 'Saving…' : 'Save API configuration'}</Button></DialogActions>
    </Dialog>

    <Snackbar open={Boolean(toast)} autoHideDuration={5000} onClose={() => setToast('')} anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}><Alert onClose={() => setToast('')} severity={toast.toLowerCase().includes('error') || toast.toLowerCase().includes('failed') ? 'error' : 'success'} variant="filled">{toast}</Alert></Snackbar>
  </div>
}

function NumbersPage({ connected, onConnect, numbers, loading, error }: { connected: boolean; onConnect: () => void; numbers: RegisteredNumber[]; loading: boolean; error: unknown }) {
  return <><PageIntro eyebrow="PHONE INVENTORY" title="Numbers" subtitle="Phone numbers registered across providers and customer accounts." />
    <Card className="surface-card" sx={{ mt: 3 }}>
      {!connected ? <ConnectPrompt onConnect={onConnect} /> : loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : numbers.length ? <Table size="small"><TableHead><TableRow>{['Phone number', 'Provider', 'Customer', 'Inbound', 'Outbound', 'Agent', 'API access'].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{numbers.map(number => <TableRow key={`${number.provider}|${number.phone_number}|${number.customer_id}`} hover><TableCell className="table-cell" sx={{ fontWeight: 700 }}>{number.phone_number}</TableCell><TableCell className="table-cell"><Chip size="small" label={number.provider} variant="outlined" sx={{ textTransform: 'capitalize' }} /></TableCell><TableCell className="table-cell">{number.customer_name}</TableCell><TableCell className="table-cell"><StatusChip enabled={number.inbound_enabled} /></TableCell><TableCell className="table-cell"><StatusChip enabled={number.outbound_enabled} /></TableCell><TableCell className="table-cell">{number.agent_id || 'Default agent'}</TableCell><TableCell className="table-cell">{number.api_key_name || 'Unassigned'}</TableCell></TableRow>)}</TableBody></Table> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><HubRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No phone numbers registered</b><div style={{ fontSize: 12, marginTop: 6 }}>Numbers appear here when customer provider integrations register them.</div></div>}
    </Card>
  </>
}

function ApiPage({ connected, onConnect, numbers, keys, loading, error, onCreate, onManage, onRevoke }: { connected: boolean; onConnect: () => void; numbers: RegisteredNumber[]; keys: PlatformApiKey[]; loading: boolean; error: unknown; onCreate: () => void; onManage: (key: PlatformApiKey) => void; onRevoke: (id: string) => void }) {
  return <><PageIntro eyebrow="DEVELOPER ACCESS" title="API" subtitle="Create a phone-number API key and manage existing keys." action={<Button variant="contained" startIcon={<AddRoundedIcon />} onClick={onCreate} disabled={!connected || !numbers.length}>Create API</Button>} />
    <Card className="surface-card" sx={{ mt: 3 }}>
      {!connected ? <ConnectPrompt onConnect={onConnect} /> : loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : keys.length ? <Table size="small"><TableHead><TableRow>{['API key', 'Phone numbers', 'Providers', 'Customer', 'Vertex AI', 'Created', 'Expires', 'State', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{keys.map(key => <TableRow key={key.id} hover><TableCell className="table-cell"><code>{key.key_prefix}</code></TableCell><TableCell className="table-cell" sx={{ fontWeight: 700 }}>{key.assigned_numbers.length ? key.assigned_numbers.map(number => number.phone_number).join(', ') : 'Unassigned'}</TableCell><TableCell className="table-cell">{key.assigned_numbers.length ? [...new Set(key.assigned_numbers.map(number => number.provider))].join(', ') : '—'}</TableCell><TableCell className="table-cell">{key.customer_name}</TableCell><TableCell className="table-cell">{key.vertex_credentials_configured ? `${key.vertex_project_id} · ${key.vertex_location}` : 'Not configured'}</TableCell><TableCell className="table-cell">{new Date(key.created_at).toLocaleDateString()}</TableCell><TableCell className="table-cell">{key.expires_at ? new Date(key.expires_at).toLocaleDateString() : 'Never'}</TableCell><TableCell className="table-cell"><StatusChip enabled={!key.revoked_at} label={key.revoked_at ? 'Revoked' : 'Active'} /></TableCell><TableCell className="table-cell">{!key.revoked_at && <><Button size="small" onClick={() => onManage(key)}>Edit</Button><Button color="error" size="small" onClick={() => onRevoke(key.id)}>Revoke</Button></>}</TableCell></TableRow>)}</TableBody></Table> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><KeyRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No API keys created</b><div style={{ fontSize: 12, marginTop: 6 }}>{numbers.length ? 'Create an API key, configure its GCP credentials, and assign phone numbers.' : 'Register an outbound-enabled phone number first.'}</div></div>}
    </Card>
  </>
}

function ConnectPrompt({ onConnect }: { onConnect: () => void }) { return <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><KeyRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>Sign in to manage the platform</b><div style={{ fontSize: 12, marginTop: 6 }}>Use your allowlisted platform administrator identity.</div><Button sx={{ mt: 1.5 }} variant="contained" onClick={onConnect}>Sign in with OIDC</Button></div> }
function StatusChip({ enabled, label }: { enabled: boolean; label?: string }) { return <Chip size="small" label={label || (enabled ? 'Enabled' : 'Disabled')} color={enabled ? 'success' : 'default'} variant={enabled ? 'filled' : 'outlined'} sx={{ fontSize: 10, height: 22 }} /> }
function PageIntro({ eyebrow, title, subtitle, action }: { eyebrow: string; title: string; subtitle: string; action?: React.ReactNode }) { return <div style={{ display: 'flex', alignItems: 'end', justifyContent: 'space-between', gap: 18, flexWrap: 'wrap' }}><div><div className="eyebrow">{eyebrow}</div><Typography className="page-title" variant="h4">{title}</Typography><div className="page-subtitle">{subtitle}</div></div>{action}</div> }
