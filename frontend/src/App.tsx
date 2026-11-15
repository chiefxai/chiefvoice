import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import axios from 'axios'
import {
  Alert, Avatar, Button, Card, Checkbox, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, Drawer, FormControl, IconButton, InputAdornment,
  FormControlLabel, InputLabel, ListItemText, Menu, MenuItem, Select, Snackbar, Stack, Switch, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Tooltip, Typography,
} from '@mui/material'
import HubRoundedIcon from '@mui/icons-material/HubRounded'
import KeyRoundedIcon from '@mui/icons-material/KeyRounded'
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import AutoAwesomeRoundedIcon from '@mui/icons-material/AutoAwesomeRounded'
import ContentCopyRoundedIcon from '@mui/icons-material/ContentCopyRounded'
import MenuRoundedIcon from '@mui/icons-material/MenuRounded'
import DarkModeRoundedIcon from '@mui/icons-material/DarkModeRounded'
import LightModeRoundedIcon from '@mui/icons-material/LightModeRounded'
import CheckCircleRoundedIcon from '@mui/icons-material/CheckCircleRounded'
import ErrorOutlineRoundedIcon from '@mui/icons-material/ErrorOutlineRounded'
import LoginPage from './LoginPage'
import { clearLoginCallbackFromUrl, currentUser, finishLogin, login, logout } from './auth'

type Page = 'Numbers' | 'Agents' | 'API' | 'Docs'
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
  credential_mode: 'platform' | 'custom' | null
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
  voice_provider?: string
  voice_model?: string
  post_call_provider?: string
  post_call_model?: string
  voice_agent_enabled?: boolean
  post_call_agent_enabled?: boolean
}
type IssuedApiKey = { key: string; api_key_record: PlatformApiKey }
type Customer = { customer_id: string; display_name: string }
type ApiCallLog = {
  session_id: string; provider: string | null; channel: string | null; direction: string | null
  caller: string | null; callee: string | null; status: string; end_reason: string | null
  started_at: string; answered_at: string | null; ended_at: string | null
  duration_seconds: number | null; transcript_available: boolean; recording_available: boolean
  post_call_status?: string | null; post_call_result?: Record<string, unknown> | null
  post_call_agent_enabled?: boolean
  webhook_status?: string | null
}
type ApiBillingUsage = {
  configured: boolean; billing_mode?: string; currency?: string; rate_per_minute_minor?: number
  calls?: number; billable_seconds?: number; charges_minor?: number; active_reservations?: number
  customer_prepaid_balance_minor?: number; customer_postpaid_used_minor?: number
  customer_postpaid_credit_limit_minor?: number; message?: string
}

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
  apiCalls: (id: string) => http.get<{ items: ApiCallLog[]; total: number }>(`/v1/admin/customers/api-keys/${id}/calls`).then(r => r.data),
  apiBilling: (id: string) => http.get<ApiBillingUsage>(`/v1/admin/customers/api-keys/${id}/billing`).then(r => r.data),
  customers: () => http.get<{ items: Customer[] }>('/v1/admin/customers').then(r => r.data.items),
  addNumbers: (data: { customer_id: string; credential_mode: 'platform' | 'custom'; phone_numbers: string[]; auth_id?: string; auth_token?: string }) => http.post('/v1/admin/customers/phone-numbers', data),
  updateNumber: (number: RegisteredNumber, inbound: boolean, outbound: boolean) => http.put(`/v1/admin/customers/phone-numbers/${encodeURIComponent(number.provider)}/${encodeURIComponent(number.phone_number)}`, { inbound_enabled: inbound, outbound_enabled: outbound }, { params: { customer_id: number.customer_id } }),
  deleteNumber: (number: RegisteredNumber) => http.delete(`/v1/admin/customers/phone-numbers/${encodeURIComponent(number.provider)}/${encodeURIComponent(number.phone_number)}`, { params: { customer_id: number.customer_id } }),
  createApiKey: (numbers: RegisteredNumber[], credentials: string, location: string, voiceProvider: string, voiceModel: string, postCallProvider: string, postCallModel: string, voiceAgentEnabled: boolean, postCallAgentEnabled: boolean) => http.post<IssuedApiKey>('/v1/admin/customers/api-keys', { phone_numbers: numbers.map(number => ({ provider: number.provider, phone_number: number.phone_number })), gcp_credentials_json: credentials || null, vertex_location: location, voice_provider: voiceProvider, voice_model: voiceModel, post_call_provider: postCallProvider, post_call_model: postCallModel, voice_agent_enabled: voiceAgentEnabled, post_call_agent_enabled: postCallAgentEnabled }).then(r => r.data),
  updateApiConfig: (id: string, numbers: RegisteredNumber[], credentials: string, location: string, voiceProvider: string, voiceModel: string, postCallProvider: string, postCallModel: string, voiceAgentEnabled: boolean, postCallAgentEnabled: boolean) => http.put(`/v1/admin/customers/api-keys/${id}/config`, { phone_numbers: numbers.map(number => ({ provider: number.provider, phone_number: number.phone_number })), gcp_credentials_json: credentials || null, vertex_location: location, voice_provider: voiceProvider, voice_model: voiceModel, post_call_provider: postCallProvider, post_call_model: postCallModel, voice_agent_enabled: voiceAgentEnabled, post_call_agent_enabled: postCallAgentEnabled }),
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
  const [voiceModel, setVoiceModel] = useState('google/gemini-live-2.5-flash-native-audio')
  const [postCallModel, setPostCallModel] = useState('gemini-2.5-flash')
  const [voiceProvider, setVoiceProvider] = useState('vertex')
  const [postCallProvider, setPostCallProvider] = useState('vertex')
  const [voiceAgentEnabled, setVoiceAgentEnabled] = useState(true)
  const [postCallAgentEnabled, setPostCallAgentEnabled] = useState(true)
  const [addNumberAnchor, setAddNumberAnchor] = useState<HTMLElement | null>(null)
  const [addNumberMode, setAddNumberMode] = useState<'platform' | 'custom' | null>(null)
  const [numberCustomerId, setNumberCustomerId] = useState('')
  const [numberInput, setNumberInput] = useState('')
  const [vobizAuthId, setVobizAuthId] = useState('')
  const [vobizAuthToken, setVobizAuthToken] = useState('')
  const [managedNumber, setManagedNumber] = useState<RegisteredNumber | null>(null)
  const [managedInbound, setManagedInbound] = useState(true)
  const [managedOutbound, setManagedOutbound] = useState(true)
  const [managementKey, setManagementKey] = useState<PlatformApiKey | null>(null)
  const [viewingKey, setViewingKey] = useState<PlatformApiKey | null>(null)
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
  const keysQuery = useQuery({ queryKey: ['platform-api-keys'], queryFn: api.apiKeys, enabled: connected && (page === 'API' || page === 'Agents'), retry: false })
  const customersQuery = useQuery({ queryKey: ['platform-customers'], queryFn: api.customers, enabled: connected && Boolean(addNumberMode), retry: false })
  const apiCallsQuery = useQuery({ queryKey: ['platform-api-calls', viewingKey?.id], queryFn: () => api.apiCalls(viewingKey!.id), enabled: connected && Boolean(viewingKey), retry: false, refetchInterval: viewingKey ? 5000 : false })
  const apiBillingQuery = useQuery({ queryKey: ['platform-api-billing', viewingKey?.id], queryFn: () => api.apiBilling(viewingKey!.id), enabled: connected && Boolean(viewingKey), retry: false, refetchInterval: viewingKey ? 10000 : false })
  const issueMutation = useMutation({
    mutationFn: ({ numbers, credentials, location, voiceProvider, voiceModel, postCallProvider, postCallModel, voiceAgentEnabled, postCallAgentEnabled }: { numbers: RegisteredNumber[]; credentials: string; location: string; voiceProvider: string; voiceModel: string; postCallProvider: string; postCallModel: string; voiceAgentEnabled: boolean; postCallAgentEnabled: boolean }) => api.createApiKey(numbers, credentials, location, voiceProvider, voiceModel, postCallProvider, postCallModel, voiceAgentEnabled, postCallAgentEnabled),
    onSuccess: result => {
      void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] })
      setOneTimeKey(result)
      setAssignmentOpen(false)
      setSelectedNumbers([])
      setGcpCredentials('')
      setVertexLocation('us-central1')
      setVoiceModel('google/gemini-live-2.5-flash-native-audio')
      setPostCallModel('gemini-2.5-flash')
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
    mutationFn: ({ key, numbers, credentials, location, voiceProvider, voiceModel, postCallProvider, postCallModel, voiceAgentEnabled, postCallAgentEnabled }: { key: PlatformApiKey; numbers: RegisteredNumber[]; credentials: string; location: string; voiceProvider: string; voiceModel: string; postCallProvider: string; postCallModel: string; voiceAgentEnabled: boolean; postCallAgentEnabled: boolean }) => api.updateApiConfig(key.id, numbers, credentials, location, voiceProvider, voiceModel, postCallProvider, postCallModel, voiceAgentEnabled, postCallAgentEnabled),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] })
      setManagementKey(null)
      setSelectedNumbers([])
      setToast('API phone number assignments updated.')
    },
    onError: error => setToast(errorText(error)),
  })
  const addNumbersMutation = useMutation({
    mutationFn: api.addNumbers,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-customers'] })
      setAddNumberMode(null); setNumberInput(''); setVobizAuthId(''); setVobizAuthToken('')
      setToast('Vobiz phone numbers added.')
    },
    onError: error => setToast(errorText(error)),
  })
  const updateNumberMutation = useMutation({
    mutationFn: ({ number, inbound, outbound }: { number: RegisteredNumber; inbound: boolean; outbound: boolean }) => api.updateNumber(number, inbound, outbound),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] }); setManagedNumber(null); setToast('Phone number settings updated.') },
    onError: error => setToast(errorText(error)),
  })
  const deleteNumberMutation = useMutation({
    mutationFn: api.deleteNumber,
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] }); void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] }); setManagedNumber(null); setToast('Phone number removed.') },
    onError: error => setToast(errorText(error)),
  })
  const currentNumbers = numbersQuery.data || []
  const outboundNumbers = currentNumbers
    .filter(number => number.outbound_enabled && !number.api_key_id)
    .filter((number, index, numbers) => numbers.findIndex(candidate => candidate.phone_number === number.phone_number) === index)
  const selected = outboundNumbers.filter(number => selectedNumbers.includes(`${number.provider}|${number.phone_number}`))
  const submitCreateApi = () => {
    if (!selected.length) { setToast('Required: select at least one unassigned outbound phone number.'); return }
    if (!voiceAgentEnabled && !postCallAgentEnabled) { setToast('Enable at least one agent for this API.'); return }
    if ((voiceAgentEnabled || postCallAgentEnabled) && !gcpCredentials.trim()) { setToast('Required: paste the Google Cloud service account JSON for enabled agents.'); return }
    if ((voiceAgentEnabled || postCallAgentEnabled) && !vertexLocation.trim()) { setToast('Required: enter the Vertex AI location.'); return }
    if ((voiceAgentEnabled && !voiceModel.trim()) || (postCallAgentEnabled && !postCallModel.trim())) { setToast('Required: enter a model ID for each enabled agent.'); return }
    issueMutation.mutate({ numbers: selected, credentials: gcpCredentials, location: vertexLocation, voiceProvider, voiceModel, postCallProvider, postCallModel, voiceAgentEnabled, postCallAgentEnabled })
  }
  const openCreateApi = () => {
    setSelectedNumbers([]); setGcpCredentials(''); setVertexLocation('us-central1')
    setVoiceProvider('vertex'); setVoiceModel('google/gemini-live-2.5-flash-native-audio')
    setPostCallProvider('vertex'); setPostCallModel('gemini-2.5-flash')
    setVoiceAgentEnabled(true); setPostCallAgentEnabled(true); setAssignmentOpen(true)
  }
  const openEditApi = (key: PlatformApiKey) => {
    setManagementKey(key); setSelectedNumbers(key.assigned_numbers.map(number => `${number.provider}|${number.phone_number}`))
    setGcpCredentials(''); setVertexLocation(key.vertex_location || 'us-central1')
    setVoiceProvider(key.voice_provider || 'vertex'); setVoiceModel(key.voice_model || 'google/gemini-live-2.5-flash-native-audio')
    setPostCallProvider(key.post_call_provider || 'vertex'); setPostCallModel(key.post_call_model || 'gemini-2.5-flash')
    setVoiceAgentEnabled(key.voice_agent_enabled ?? true); setPostCallAgentEnabled(key.post_call_agent_enabled ?? true)
  }
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
  const navigation = <><div className="brand"><div className="brand-mark"><HubRoundedIcon fontSize="small" /></div><div className="brand-word">chief<span>voice</span></div></div><div className="nav-caption">Platform</div><div className="nav-list">{(['Numbers', 'Agents', 'API', 'Docs'] as const).map(item => <Button key={item} className={`nav-item ${page === item ? 'active' : ''}`} onClick={() => { setPage(item); setDrawerOpen(false) }} startIcon={item === 'Numbers' ? <HubRoundedIcon /> : item === 'Agents' ? <AutoAwesomeRoundedIcon /> : item === 'API' ? <KeyRoundedIcon /> : <ContentCopyRoundedIcon />}>{item}</Button>)}</div></>

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
        {page === 'Numbers' && <NumbersPage connected={connected} onConnect={() => { void connect() }} numbers={currentNumbers} loading={numbersQuery.isLoading} error={numbersQuery.error} onAddNumber={event => setAddNumberAnchor(event.currentTarget)} onManageNumber={number => { setManagedNumber(number); setManagedInbound(number.inbound_enabled); setManagedOutbound(number.outbound_enabled) }} />}
        {page === 'API' && <ApiPage connected={connected} numbers={outboundNumbers} keys={keysQuery.data || []} loading={keysQuery.isLoading} error={keysQuery.error} onCreate={openCreateApi} onView={key => setViewingKey(key)} onManage={openEditApi} onRevoke={id => { if (window.confirm('Revoke this API key? Requests using it will stop working immediately.')) revokeMutation.mutate(id) }} />}
        {page === 'Agents' && <AgentsPage keys={keysQuery.data || []} loading={keysQuery.isLoading} error={keysQuery.error} onCreate={() => { setPage('API'); openCreateApi() }} onEdit={openEditApi} />}
        {page === 'Docs' && <DocsPage />}
      </div>
    </main>

    <Menu anchorEl={addNumberAnchor} open={Boolean(addNumberAnchor)} onClose={() => setAddNumberAnchor(null)}>
      <MenuItem onClick={() => { setAddNumberAnchor(null); setNumberCustomerId(''); setAddNumberMode('platform') }}><ListItemText primary="Add number with platform Vobiz account" secondary="Enter phone numbers; the server uses its configured Vobiz credentials." /></MenuItem>
      <MenuItem onClick={() => { setAddNumberAnchor(null); setNumberCustomerId(''); setAddNumberMode('custom') }}><ListItemText primary="Set up customer Vobiz account" secondary="Choose Vobiz, enter credentials, then add one or more numbers." /></MenuItem>
    </Menu>

    <Dialog open={Boolean(addNumberMode)} onClose={() => setAddNumberMode(null)} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>{addNumberMode === 'custom' ? 'Configure Vobiz numbers' : 'Add phone numbers'}</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        {addNumberMode === 'custom' && <Alert severity="info">Provider selection: <b>Vobiz</b>. Credentials are stored encrypted and apply to this customer’s Vobiz numbers.</Alert>}
        {addNumberMode === 'platform' && <Alert severity="info">The server’s Vobiz account credentials will be used. Configure <code>VOBIZ_AUTH_ID</code> and <code>VOBIZ_AUTH_TOKEN</code> on the backend.</Alert>}
        <FormControl fullWidth>
          <InputLabel id="number-customer-label">Customer</InputLabel>
          <Select labelId="number-customer-label" label="Customer" value={numberCustomerId} onChange={event => setNumberCustomerId(event.target.value)}>
            {(customersQuery.data || []).map(customer => <MenuItem key={customer.customer_id} value={customer.customer_id}>{customer.display_name}</MenuItem>)}
          </Select>
        </FormControl>
        {addNumberMode === 'custom' && <>
          <TextField label="Vobiz Auth ID" value={vobizAuthId} onChange={event => setVobizAuthId(event.target.value)} fullWidth autoComplete="off" />
          <TextField label="Vobiz Auth Token" type="password" value={vobizAuthToken} onChange={event => setVobizAuthToken(event.target.value)} fullWidth autoComplete="new-password" />
        </>}
        <TextField label="Phone numbers" value={numberInput} onChange={event => setNumberInput(event.target.value)} multiline minRows={4} fullWidth placeholder={'+15551234567\n+15557654321'} helperText="Enter one number per line in international format. You can add several numbers at once." />
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setAddNumberMode(null)}>Cancel</Button><Button variant="contained" disabled={!numberCustomerId || !numberInput.trim() || (addNumberMode === 'custom' && (!vobizAuthId.trim() || !vobizAuthToken.trim())) || addNumbersMutation.isPending} onClick={() => {
        const phoneNumbers = [...new Set(numberInput.split(/[\n,;\s]+/).map(value => value.trim()).filter(Boolean))]
        addNumbersMutation.mutate({ customer_id: numberCustomerId, credential_mode: addNumberMode || 'platform', phone_numbers: phoneNumbers, ...(addNumberMode === 'custom' ? { auth_id: vobizAuthId, auth_token: vobizAuthToken } : {}) })
      }}>{addNumbersMutation.isPending ? 'Adding…' : 'Add numbers'}</Button></DialogActions>
    </Dialog>

    <Dialog open={Boolean(managedNumber)} onClose={() => setManagedNumber(null)} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>Manage phone number</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Typography><b>{managedNumber?.phone_number}</b> · {managedNumber?.provider} · {managedNumber?.customer_name}</Typography>
        <FormControl fullWidth><InputLabel id="inbound-number-label">Inbound calls</InputLabel><Select labelId="inbound-number-label" label="Inbound calls" value={managedInbound ? 'enabled' : 'disabled'} onChange={event => setManagedInbound(event.target.value === 'enabled')}><MenuItem value="enabled">Enabled</MenuItem><MenuItem value="disabled">Disabled</MenuItem></Select></FormControl>
        <FormControl fullWidth><InputLabel id="outbound-number-label">Outbound calls</InputLabel><Select labelId="outbound-number-label" label="Outbound calls" value={managedOutbound ? 'enabled' : 'disabled'} onChange={event => setManagedOutbound(event.target.value === 'enabled')}><MenuItem value="enabled">Enabled</MenuItem><MenuItem value="disabled">Disabled</MenuItem></Select></FormControl>
        <Typography variant="caption">Credential mode: {managedNumber?.credential_mode || 'Vobiz integration'}</Typography>
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5, justifyContent: 'space-between' }}><Button color="error" disabled={!managedNumber || deleteNumberMutation.isPending} onClick={() => { if (managedNumber && window.confirm(`Remove ${managedNumber.phone_number}? This will release the number from any API key.`)) deleteNumberMutation.mutate(managedNumber) }}>Remove number</Button><Stack direction="row" gap={1}><Button onClick={() => setManagedNumber(null)}>Cancel</Button><Button variant="contained" disabled={!managedNumber || (!managedInbound && !managedOutbound) || updateNumberMutation.isPending} onClick={() => managedNumber && updateNumberMutation.mutate({ number: managedNumber, inbound: managedInbound, outbound: managedOutbound })}>{updateNumberMutation.isPending ? 'Saving…' : 'Save settings'}</Button></Stack></DialogActions>
    </Dialog>

    <Dialog open={assignmentOpen} onClose={() => setAssignmentOpen(false)} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>Create API and configure agents</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="info" sx={{ fontSize: 12 }}>Choose one or more unassigned phone numbers, then configure separate calling and post-call agents. Each API stores its own encrypted Google Cloud service account credentials.</Alert>
        <Typography variant="h6" sx={{ fontWeight: 800 }}>Phone numbers</Typography>
        <FormControl fullWidth disabled={!outboundNumbers.length}>
          <InputLabel id="assigned-number-label">Phone numbers</InputLabel>
          <Select multiple labelId="assigned-number-label" value={selectedNumbers} label="Phone numbers" onChange={event => setSelectedNumbers(typeof event.target.value === 'string' ? event.target.value.split(',') : event.target.value)} renderValue={values => `${values.length} phone number${values.length === 1 ? '' : 's'} selected`}>
            {outboundNumbers.map(number => <MenuItem key={`${number.provider}|${number.phone_number}`} value={`${number.provider}|${number.phone_number}`} disabled={Boolean(selected.length && selected[0].customer_id !== number.customer_id)}><Checkbox checked={selectedNumbers.includes(`${number.provider}|${number.phone_number}`)} size="small" /><ListItemText primary={`${number.phone_number} · ${number.provider}`} secondary={number.customer_name} /></MenuItem>)}
          </Select>
        </FormControl>
        {!outboundNumbers.length && <Alert severity="warning">No unassigned outbound number is available. Add a number in Numbers first; this API can be created once at least one number is selected.</Alert>}
        <Typography variant="h6" sx={{ mt: 1, fontWeight: 800 }}>Calling agent</Typography>
        <FormControlLabel control={<Switch checked={voiceAgentEnabled} onChange={event => setVoiceAgentEnabled(event.target.checked)} />} label={voiceAgentEnabled ? 'Calling agent enabled' : 'Calling agent disabled'} />
        {voiceAgentEnabled ? <Stack direction={{ xs: 'column', sm: 'row' }} gap={2}>
          <FormControl fullWidth><InputLabel id="create-voice-provider-label">Provider</InputLabel><Select labelId="create-voice-provider-label" label="Provider" value={voiceProvider} onChange={event => setVoiceProvider(event.target.value)}><MenuItem value="vertex">Google Vertex AI</MenuItem></Select></FormControl>
          <TextField label="Live voice model ID" value={voiceModel} onChange={event => setVoiceModel(event.target.value)} fullWidth />
        </Stack> : <Alert severity="warning">Outbound AI calls from this API will be rejected until the calling agent is enabled.</Alert>}
        <Typography variant="h6" sx={{ mt: 1, fontWeight: 800 }}>Post-call agent</Typography>
        <FormControlLabel control={<Switch checked={postCallAgentEnabled} onChange={event => setPostCallAgentEnabled(event.target.checked)} />} label={postCallAgentEnabled ? 'Post-call agent enabled' : 'Post-call agent disabled'} />
        {postCallAgentEnabled && <Stack direction={{ xs: 'column', sm: 'row' }} gap={2}>
          <FormControl fullWidth><InputLabel id="create-post-provider-label">Provider</InputLabel><Select labelId="create-post-provider-label" label="Provider" value={postCallProvider} onChange={event => setPostCallProvider(event.target.value)}><MenuItem value="vertex">Google Vertex AI</MenuItem></Select></FormControl>
          <TextField label="Post-call model ID" value={postCallModel} onChange={event => setPostCallModel(event.target.value)} fullWidth />
        </Stack>}
        {(voiceAgentEnabled || postCallAgentEnabled) ? <>
          <Typography variant="h6" sx={{ mt: 1, fontWeight: 800 }}>Google Cloud credentials</Typography>
          <TextField label="Google Cloud service account JSON" value={gcpCredentials} onChange={event => setGcpCredentials(event.target.value)} multiline minRows={5} fullWidth placeholder={'Paste the downloaded service account JSON key'} />
          <TextField label="Vertex AI location" value={vertexLocation} onChange={event => setVertexLocation(event.target.value)} fullWidth placeholder="us-central1" />
        </> : <Alert severity="info">Both agents are disabled. This API will not start outbound AI calls or run post-call analysis.</Alert>}
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setAssignmentOpen(false)}>Cancel</Button><Button variant="contained" disabled={issueMutation.isPending || (!voiceAgentEnabled && !postCallAgentEnabled)} onClick={submitCreateApi}>{issueMutation.isPending ? 'Creating…' : 'Create API'}</Button></DialogActions>
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
        <Alert severity="info" sx={{ fontSize: 12 }}>Saved service account credentials are encrypted. Leave the credential field blank to keep the current key; enter new JSON to rotate it. Provider choices reflect the integrations currently supported by the backend.</Alert>
        <Typography variant="h6" sx={{ mt: 1, fontWeight: 800 }}>Calling agent</Typography>
        <FormControlLabel control={<Switch checked={voiceAgentEnabled} onChange={event => setVoiceAgentEnabled(event.target.checked)} />} label={voiceAgentEnabled ? 'Calling agent enabled' : 'Calling agent disabled'} />
        {voiceAgentEnabled ? <Stack direction={{ xs: 'column', sm: 'row' }} gap={2}>
          <FormControl fullWidth><InputLabel id="edit-voice-provider-label">Provider</InputLabel><Select labelId="edit-voice-provider-label" label="Provider" value={voiceProvider} onChange={event => setVoiceProvider(event.target.value)}><MenuItem value="vertex">Google Vertex AI</MenuItem></Select></FormControl>
          <TextField label="Live voice model ID" value={voiceModel} onChange={event => setVoiceModel(event.target.value)} fullWidth />
        </Stack> : <Alert severity="warning">Outbound AI calls from this API will be rejected until the calling agent is enabled.</Alert>}
        <Typography variant="h6" sx={{ mt: 1, fontWeight: 800 }}>Post-call agent</Typography>
        <FormControlLabel control={<Switch checked={postCallAgentEnabled} onChange={event => setPostCallAgentEnabled(event.target.checked)} />} label={postCallAgentEnabled ? 'Post-call agent enabled' : 'Post-call agent disabled'} />
        {postCallAgentEnabled && <Stack direction={{ xs: 'column', sm: 'row' }} gap={2}>
          <FormControl fullWidth><InputLabel id="edit-post-provider-label">Provider</InputLabel><Select labelId="edit-post-provider-label" label="Provider" value={postCallProvider} onChange={event => setPostCallProvider(event.target.value)}><MenuItem value="vertex">Google Vertex AI</MenuItem></Select></FormControl>
          <TextField label="Post-call model ID" value={postCallModel} onChange={event => setPostCallModel(event.target.value)} fullWidth />
        </Stack>}
        {(voiceAgentEnabled || postCallAgentEnabled) ? <>
          <Typography variant="h6" sx={{ mt: 1, fontWeight: 800 }}>Google Cloud credentials</Typography>
          <TextField label="Replace Google Cloud service account JSON (optional)" value={gcpCredentials} onChange={event => setGcpCredentials(event.target.value)} multiline minRows={4} fullWidth placeholder={managementKey?.vertex_credentials_configured ? 'Credentials are saved. Leave blank to keep them.' : 'Paste service account JSON'} />
          <TextField label="Vertex AI location" value={vertexLocation} onChange={event => setVertexLocation(event.target.value)} fullWidth />
        </> : <Alert severity="info">Both agents are disabled. This API will not start outbound AI calls or run post-call analysis.</Alert>}
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
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setManagementKey(null)}>Cancel</Button><Button variant="contained" disabled={!managementKey || !selectedNumbers.length || (!voiceAgentEnabled && !postCallAgentEnabled) || ((voiceAgentEnabled || postCallAgentEnabled) && !gcpCredentials.trim() && !managementKey.vertex_credentials_configured) || (voiceAgentEnabled && !voiceModel.trim()) || (postCallAgentEnabled && !postCallModel.trim()) || manageNumbersMutation.isPending} onClick={() => { if (!managementKey) return; const numbers = currentNumbers.filter(number => selectedNumbers.includes(`${number.provider}|${number.phone_number}`)); manageNumbersMutation.mutate({ key: managementKey, numbers, credentials: gcpCredentials, location: vertexLocation, voiceProvider, voiceModel, postCallProvider, postCallModel, voiceAgentEnabled, postCallAgentEnabled }) }}>{manageNumbersMutation.isPending ? 'Saving…' : 'Save API configuration'}</Button></DialogActions>
    </Dialog>

    <Dialog open={Boolean(viewingKey)} onClose={() => setViewingKey(null)} maxWidth="lg" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>API details · {viewingKey?.name}</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Stack direction={{ xs: 'column', sm: 'row' }} gap={1.5} flexWrap="wrap">
          <Card variant="outlined" sx={{ p: 1.5, flex: 1, minWidth: 180 }}><Typography variant="caption">API key</Typography><Typography><code>{viewingKey?.key_prefix}</code></Typography></Card>
          <Card variant="outlined" sx={{ p: 1.5, flex: 1, minWidth: 180 }}><Typography variant="caption">Assigned numbers</Typography><Typography>{viewingKey?.assigned_numbers.map(item => item.phone_number).join(', ') || 'None'}</Typography></Card>
          <Card variant="outlined" sx={{ p: 1.5, flex: 1, minWidth: 180 }}><Typography variant="caption">Calling agent</Typography><Typography>{viewingKey?.voice_agent_enabled === false ? 'Disabled' : `${viewingKey?.voice_provider || 'Vertex AI'} · ${viewingKey?.voice_model || 'Default model'}`}</Typography></Card>
          <Card variant="outlined" sx={{ p: 1.5, flex: 1, minWidth: 180 }}><Typography variant="caption">Post-call agent</Typography><Typography>{viewingKey?.post_call_agent_enabled === false ? 'Disabled' : `${viewingKey?.post_call_provider || 'Vertex AI'} · ${viewingKey?.post_call_model || 'Default model'}`}</Typography></Card>
        </Stack>
        <Typography variant="h6" sx={{ fontWeight: 800 }}>Calls for this API</Typography>
        {apiCallsQuery.isLoading ? <CircularProgress size={22} /> : apiCallsQuery.error ? <Alert severity="error">{errorText(apiCallsQuery.error)}</Alert> : apiCallsQuery.data?.items.length ? <Table size="small"><TableHead><TableRow>{['Started', 'Direction', 'From → To', 'Provider', 'Status', 'Duration', 'Post-call result', 'Session'].map(item => <TableCell className="table-head" key={item}>{item}</TableCell>)}</TableRow></TableHead><TableBody>{apiCallsQuery.data.items.map(call => <TableRow key={call.session_id}><TableCell className="table-cell">{new Date(call.started_at).toLocaleString()}</TableCell><TableCell className="table-cell">{call.direction || '—'}</TableCell><TableCell className="table-cell">{call.caller || '—'} → {call.callee || '—'}</TableCell><TableCell className="table-cell">{call.provider || '—'}</TableCell><TableCell className="table-cell"><StatusChip enabled={call.status === 'completed'} label={call.status} /></TableCell><TableCell className="table-cell">{call.duration_seconds == null ? '—' : `${call.duration_seconds}s`}</TableCell><TableCell className="table-cell">{call.post_call_status ? <><StatusChip enabled={call.post_call_status === 'completed'} label={call.post_call_status} />{call.post_call_result && <details style={{ marginTop: 6 }}><summary>View result</summary><pre style={{ whiteSpace: 'pre-wrap', maxWidth: 320, fontSize: 11 }}>{JSON.stringify(call.post_call_result, null, 2)}</pre></details>}</> : call.post_call_agent_enabled === false ? <StatusChip enabled={false} label="disabled" /> : 'Waiting for call to finish'}</TableCell><TableCell className="table-cell"><code>{call.session_id}</code></TableCell></TableRow>)}</TableBody></Table> : <div className="empty-state">No calls yet. Use the API key and the request example on the Docs page to place a test call.</div>}
        <Typography variant="h6" sx={{ fontWeight: 800, mt: 1 }}>Billing for this API</Typography>
        {apiBillingQuery.isLoading ? <CircularProgress size={22} /> : apiBillingQuery.error ? <Alert severity="error">{errorText(apiBillingQuery.error)}</Alert> : apiBillingQuery.data?.configured ? <>
          <Alert severity="info">Call charges below are attributed to this API key. Pricing and the prepaid balance or postpaid credit limit are managed on the customer billing plan and shared by API keys under that customer.</Alert>
          <Stack direction={{ xs: 'column', sm: 'row' }} gap={1.5} flexWrap="wrap">
            <UsageCard label="Calls" value={String(apiBillingQuery.data.calls ?? 0)} />
            <UsageCard label="Billable time" value={`${Math.floor((apiBillingQuery.data.billable_seconds || 0) / 60)}m ${(apiBillingQuery.data.billable_seconds || 0) % 60}s`} />
            <UsageCard label="Attributed charges" value={formatMoney(apiBillingQuery.data.charges_minor || 0, apiBillingQuery.data.currency || 'USD')} />
            <UsageCard label="Customer plan" value={`${apiBillingQuery.data.billing_mode} · ${formatMoney(apiBillingQuery.data.rate_per_minute_minor || 0, apiBillingQuery.data.currency || 'USD')}/min`} />
            <UsageCard label="Shared customer balance" value={apiBillingQuery.data.billing_mode === 'prepaid' ? formatMoney(apiBillingQuery.data.customer_prepaid_balance_minor || 0, apiBillingQuery.data.currency || 'USD') : `${formatMoney(apiBillingQuery.data.customer_postpaid_used_minor || 0, apiBillingQuery.data.currency || 'USD')} used / ${formatMoney(apiBillingQuery.data.customer_postpaid_credit_limit_minor || 0, apiBillingQuery.data.currency || 'USD')} limit`} />
          </Stack>
        </> : <Alert severity="warning">{apiBillingQuery.data?.message || 'No billing plan is configured for this customer yet.'}</Alert>}
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setViewingKey(null)}>Close</Button><Button onClick={() => { if (viewingKey) openEditApi(viewingKey); setViewingKey(null) }}>Edit API</Button></DialogActions>
    </Dialog>

    <Snackbar open={Boolean(toast)} autoHideDuration={5000} onClose={() => setToast('')} anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}><Alert onClose={() => setToast('')} severity={toast.toLowerCase().includes('error') || toast.toLowerCase().includes('failed') || toast.toLowerCase().includes('required') ? 'error' : 'success'} variant="filled">{toast}</Alert></Snackbar>
  </div>
}

function NumbersPage({ connected, onConnect, numbers, loading, error, onAddNumber, onManageNumber }: { connected: boolean; onConnect: () => void; numbers: RegisteredNumber[]; loading: boolean; error: unknown; onAddNumber: (event: React.MouseEvent<HTMLButtonElement>) => void; onManageNumber: (number: RegisteredNumber) => void }) {
  return <><PageIntro eyebrow="PHONE INVENTORY" title="Numbers" subtitle="Phone numbers registered across providers and customer accounts." action={<Button variant="contained" startIcon={<AddRoundedIcon />} onClick={onAddNumber} disabled={!connected}>Add number</Button>} />
    <Card className="surface-card" sx={{ mt: 3 }}>
      {!connected ? <ConnectPrompt onConnect={onConnect} /> : loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : numbers.length ? <Table size="small"><TableHead><TableRow>{['Phone number', 'Provider', 'Customer', 'Credentials', 'Inbound', 'Outbound', 'Agent', 'API access', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{numbers.map(number => <TableRow key={`${number.provider}|${number.phone_number}|${number.customer_id}`} hover><TableCell className="table-cell" sx={{ fontWeight: 700 }}>{number.phone_number}</TableCell><TableCell className="table-cell"><Chip size="small" label={number.provider} variant="outlined" sx={{ textTransform: 'capitalize' }} /></TableCell><TableCell className="table-cell">{number.customer_name}</TableCell><TableCell className="table-cell">{number.credential_mode === 'platform' ? 'Platform' : number.credential_mode === 'custom' ? 'Customer' : '—'}</TableCell><TableCell className="table-cell"><StatusChip enabled={number.inbound_enabled} /></TableCell><TableCell className="table-cell"><StatusChip enabled={number.outbound_enabled} /></TableCell><TableCell className="table-cell">{number.agent_id || 'Default agent'}</TableCell><TableCell className="table-cell">{number.api_key_name || 'Unassigned'}</TableCell><TableCell className="table-cell"><Button size="small" onClick={() => onManageNumber(number)}>Manage</Button></TableCell></TableRow>)}</TableBody></Table> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><HubRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No phone numbers registered</b><div style={{ fontSize: 12, marginTop: 6 }}>Add a number to get started. You can use the platform Vobiz account or configure customer credentials.</div></div>}
    </Card>
  </>
}

function ApiPage({ connected, numbers, keys, loading, error, onCreate, onView, onManage, onRevoke }: { connected: boolean; numbers: RegisteredNumber[]; keys: PlatformApiKey[]; loading: boolean; error: unknown; onCreate: () => void; onView: (key: PlatformApiKey) => void; onManage: (key: PlatformApiKey) => void; onRevoke: (id: string) => void }) {
  return <><PageIntro eyebrow="DEVELOPER ACCESS" title="API" subtitle="Create a phone-number API key and manage existing keys." action={<Button variant="contained" startIcon={<AddRoundedIcon />} onClick={onCreate} disabled={!connected}>Create API</Button>} />
    <Card className="surface-card" sx={{ mt: 3 }}>
      {loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : keys.length ? <Table size="small"><TableHead><TableRow>{['API key', 'Phone numbers', 'Providers', 'Customer', 'Vertex AI', 'Created', 'Expires', 'State', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{keys.map(key => <TableRow key={key.id} hover><TableCell className="table-cell"><code>{key.key_prefix}</code></TableCell><TableCell className="table-cell" sx={{ fontWeight: 700 }}>{key.assigned_numbers.length ? key.assigned_numbers.map(number => number.phone_number).join(', ') : 'Unassigned'}</TableCell><TableCell className="table-cell">{key.assigned_numbers.length ? [...new Set(key.assigned_numbers.map(number => number.provider))].join(', ') : '—'}</TableCell><TableCell className="table-cell">{key.customer_name}</TableCell><TableCell className="table-cell">{key.vertex_credentials_configured ? `${key.vertex_project_id} · ${key.vertex_location}` : 'Not configured'}</TableCell><TableCell className="table-cell">{new Date(key.created_at).toLocaleDateString()}</TableCell><TableCell className="table-cell">{key.expires_at ? new Date(key.expires_at).toLocaleDateString() : 'Never'}</TableCell><TableCell className="table-cell"><StatusChip enabled={!key.revoked_at} label={key.revoked_at ? 'Revoked' : 'Active'} /></TableCell><TableCell className="table-cell"><Button size="small" onClick={() => onView(key)}>View</Button>{!key.revoked_at && <><Button size="small" onClick={() => onManage(key)}>Edit</Button><Button color="error" size="small" onClick={() => onRevoke(key.id)}>Revoke</Button></>}</TableCell></TableRow>)}</TableBody></Table> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><KeyRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No API keys created</b><div style={{ fontSize: 12, marginTop: 6 }}>{numbers.length ? 'Create an API key, configure its GCP credentials, and assign phone numbers.' : 'Register an outbound-enabled phone number first.'}</div></div>}
    </Card>
  </>
}

function AgentsPage({ keys, loading, error, onEdit, onCreate }: { keys: PlatformApiKey[]; loading: boolean; error: unknown; onEdit: (key: PlatformApiKey) => void; onCreate: () => void }) {
  return <><PageIntro eyebrow="AGENT CONFIGURATION" title="Agents" subtitle="Choose the live calling and post-call model providers for each API." action={<Button variant="contained" startIcon={<AddRoundedIcon />} onClick={onCreate}>Configure agents</Button>} />
    <Alert severity="info" sx={{ mt: 2 }}>Vertex AI is available for both agent types today. Provider choices are separate so live voice and post-call processing can move to different providers as integrations are added.</Alert>
    <Card className="surface-card" sx={{ mt: 2 }}>
      {loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : keys.length ? <Table size="small"><TableHead><TableRow>{['API / customer', 'Calling agent', 'Post-call agent', 'Phone numbers', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{keys.filter(key => !key.revoked_at).map(key => <TableRow key={key.id} hover><TableCell className="table-cell"><b>{key.name}</b><br /><Typography variant="caption">{key.customer_name} · <code>{key.key_prefix}</code></Typography></TableCell><TableCell className="table-cell"><Chip size="small" label={key.voice_agent_enabled === false ? 'Disabled' : key.voice_provider === 'vertex' ? 'Google Vertex AI' : key.voice_provider || 'Not configured'} /><Typography variant="body2" sx={{ mt: 0.5 }}>{key.voice_agent_enabled === false ? 'Outbound calling unavailable' : key.voice_model || 'Default voice model'}</Typography></TableCell><TableCell className="table-cell"><Chip size="small" label={key.post_call_agent_enabled === false ? 'Disabled' : key.post_call_provider === 'vertex' ? 'Google Vertex AI' : key.post_call_provider || 'Not configured'} /><Typography variant="body2" sx={{ mt: 0.5 }}>{key.post_call_agent_enabled === false ? 'Analysis skipped' : key.post_call_model || 'Default post-call model'}</Typography></TableCell><TableCell className="table-cell">{key.assigned_numbers.map(item => item.phone_number).join(', ') || 'None'}</TableCell><TableCell className="table-cell"><Button size="small" onClick={() => onEdit(key)}>Configure</Button></TableCell></TableRow>)}</TableBody></Table> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><AutoAwesomeRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No API agents configured</b><div style={{ fontSize: 12, marginTop: 6 }}>Create an API and choose its calling and post-call agent providers and models.</div><Button variant="contained" sx={{ mt: 2 }} onClick={onCreate}>Configure agents</Button></div>}
    </Card>
  </>
}

function ConnectPrompt({ onConnect }: { onConnect: () => void }) { return <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><KeyRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>Sign in to manage the platform</b><div style={{ fontSize: 12, marginTop: 6 }}>Use your allowlisted platform administrator identity.</div><Button sx={{ mt: 1.5 }} variant="contained" onClick={onConnect}>Sign in with OIDC</Button></div> }
function UsageCard({ label, value }: { label: string; value: string }) { return <Card variant="outlined" sx={{ p: 1.5, flex: 1, minWidth: 150 }}><Typography variant="caption" color="text.secondary">{label}</Typography><Typography sx={{ fontWeight: 800, mt: 0.4 }}>{value}</Typography></Card> }
function formatMoney(amountMinor: number, currency: string) { return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(amountMinor / 100) }
function DocsPage() {
  const callBody = `curl -X POST "$CHIEFVOICE_URL/v1/calls/outbound" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: appointment-123" \\
  -d '{
    "provider": "vobiz",
    "channel": "phone",
    "destination": "+15551234567",
    "caller_id": "+15557654321",
    "live_agent": {
      "agent_id": "agent-profile-id",
      "context": {"patient_name": "Avery", "appointment": "Tuesday 10:30"}
    },
    "post_call": {
      "context": "Extract the appointment confirmation. Use unknown when not stated.",
      "output_schema": {
        "type": "object",
        "properties": {
          "confirmed": {"type": "boolean"},
          "preferred_time": {"type": "string", "enum": ["morning", "afternoon", "unknown"]}
        },
        "required": ["confirmed", "preferred_time"],
        "additionalProperties": false
      },
      "tools": ["time"],
      "time_zone": "Asia/Kolkata",
      "time_format": "%Y-%m-%dT%H:%M:%S%z",
      "include_live_agent_context": true
    }
  }'`
  return <><PageIntro eyebrow="DEVELOPER GUIDE" title="API documentation" subtitle="Start a call, check its progress, and receive structured post-call results." />
    <Stack gap={2} sx={{ mt: 3 }}>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Authentication and phone numbers</Typography><Typography sx={{ mt: 1 }}>Use the full API key shown once when the API was created. Send it as a Bearer token. It can only call from phone numbers assigned to that API. If you assigned multiple numbers for a provider, include <code>caller_id</code>.</Typography><Typography sx={{ mt: 1 }}>Create the API key in the API page. Credentials and phone number assignments are editable from the API’s detail view.</Typography></Card>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Start an outbound call</Typography><Typography sx={{ mt: 1, mb: 1.5 }}>The call request returns an accepted session ID quickly; it does not hold the HTTP connection open while the conversation runs.</Typography><TextField value={callBody} fullWidth multiline minRows={20} InputProps={{ readOnly: true }} /></Card>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Check call status and results</Typography><Typography sx={{ mt: 1 }}>Poll <code>GET /v1/calls/{'{session_id}'}</code> for lifecycle status and <code>GET /v1/calls/{'{session_id}'}/post-call</code> for analysis status and result. Post-call processing starts after the call finishes. The API view refreshes its call log automatically while open.</Typography><TextField value={`curl "$CHIEFVOICE_URL/v1/calls/$SESSION_ID" \\\n  -H "Authorization: Bearer $CHIEFVOICE_API_KEY"`} fullWidth multiline minRows={2} sx={{ mt: 1.5 }} InputProps={{ readOnly: true }} /></Card>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Context, output schema, and tools</Typography><Typography sx={{ mt: 1 }}>Keep live conversation context in <code>live_agent.context</code>. Put extraction instructions and the JSON output schema in <code>post_call</code>. The schema is validated with strict types before the result is stored or delivered. Supported post-call tools are <code>calculator</code> and <code>time</code>; when using the time tool, set an IANA timezone and a format containing <code>%z</code>.</Typography><Typography sx={{ mt: 1 }}>Use <code>include_live_agent_context</code> only when post-call extraction needs the live context too. Treat supplied context as reference data and provide an “unknown” option for facts the agent cannot establish.</Typography></Card>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Webhooks and polling</Typography><Typography sx={{ mt: 1 }}>Polling is available for call and post-call status. If a post-call webhook is configured, the platform sends a signed callback when analysis is ready. The recipient should deduplicate deliveries by event ID because failed delivery attempts are retried.</Typography></Card>
    </Stack>
  </>
}
function StatusChip({ enabled, label }: { enabled: boolean; label?: string }) { return <Chip size="small" label={label || (enabled ? 'Enabled' : 'Disabled')} color={enabled ? 'success' : 'default'} variant={enabled ? 'filled' : 'outlined'} sx={{ fontSize: 10, height: 22 }} /> }
function PageIntro({ eyebrow, title, subtitle, action }: { eyebrow: string; title: string; subtitle: string; action?: React.ReactNode }) { return <div style={{ display: 'flex', alignItems: 'end', justifyContent: 'space-between', gap: 18, flexWrap: 'wrap' }}><div><div className="eyebrow">{eyebrow}</div><Typography className="page-title" variant="h4">{title}</Typography><div className="page-subtitle">{subtitle}</div></div>{action}</div> }
