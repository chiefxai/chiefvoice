import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import axios from 'axios'
import {
  Alert, Avatar, Button, Card, Checkbox, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, Drawer, FormControl, IconButton, InputAdornment,
  InputLabel, ListItemText, Menu, MenuItem, Select, Snackbar, Stack, Switch, FormControlLabel, Table, TableBody, TableCell,
  TableHead, TableRow, TableContainer, TablePagination, TextField, Tooltip, Typography,
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
import ScienceRoundedIcon from '@mui/icons-material/ScienceRounded'
import { useLocation, useNavigate } from 'react-router-dom'
import LoginPage from './LoginPage'
import { clearLoginCallbackFromUrl, currentUser, finishLogin, login, logout } from './auth'

type Page = 'Numbers' | 'Agents' | 'API' | 'Simulation' | 'Docs'
const pagePaths: Record<Page, string> = {
  Numbers: '/numbers', Agents: '/agents', API: '/api', Simulation: '/simulation', Docs: '/docs',
}
const defaultSimulationPostCallContext = 'Summarize the call and extract the requested fields. Use unknown when information is not stated; do not guess.'
const defaultSimulationOutputSchema = JSON.stringify({ type: 'object', properties: { summary: { type: 'string' }, outcome: { type: 'string', enum: ['completed', 'follow_up_needed', 'unknown'] } }, required: ['summary', 'outcome'] }, null, 2)
const livePromptContainsPostCallDirective = (instructions: string) =>
  instructions.split(/[\n.!?]+/).some(clause => {
    if (/\b(?:do not|don't|never|avoid|must not|should not)\b/i.test(clause)) return false
    return (
      /\b(?:summari[sz]e|analy[sz]e)\b.{0,80}\b(?:the )?call\b/i.test(clause) ||
      /\b(?:extract|transcribe)\b.{0,100}\b(?:transcript|from the call)\b/i.test(clause) ||
      /\b(?:post[- ]call|after the call|following the call)\b.{0,100}\b(?:extract|summari[sz]e|analy[sz]e|transcribe|return|output)\b/i.test(clause) ||
      /\b(?:extract|summari[sz]e|analy[sz]e|transcribe)\b.{0,100}\b(?:post[- ]call|after the call|following the call)\b/i.test(clause)
    )
  })
const pageFromPath = (pathname: string): Page => {
  const match = (Object.entries(pagePaths) as [Page, string][]).find(([, path]) => path === pathname.replace(/\/$/, ''))
  return match?.[0] || 'Numbers'
}
type RegisteredNumber = {
  provider: string
  phone_number: string
  customer_id: string
  customer_name: string
  inbound_enabled: boolean
  outbound_enabled: boolean
  standard_channels: number
  unlimited_channels: number
  agent_id: string | null
  api_key_id: string | null
  api_key_name: string | null
  credential_mode: 'platform' | 'custom' | null
  tags: string[]
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
  voice_location?: string | null
  post_call_location?: string | null
  vertex_credentials_configured?: boolean
  voice_provider?: string
  voice_model?: string
  post_call_provider?: string
  post_call_model?: string
  voice_agent_enabled?: boolean
  post_call_agent_enabled?: boolean
  voice_agent_id?: string | null
  post_call_agent_id?: string | null
  recording_allowed?: boolean
  tags: string[]
}
type AgentConfiguration = {
  id: string; owner: 'platform'; name: string
  kind: 'calling' | 'post_call'; provider: string; model: string; location: string
  cost_per_minute_minor: number; currency: string
  auto_hangup_when_complete: boolean
  enabled: boolean
  tags: string[]
}
type IssuedApiKey = {
  key: string
  api_key_record: PlatformApiKey
  post_call_webhook?: { target_url: string; signing_secret: string; configured_at: string } | null
  webhook_setup_error?: string | null
}
type Customer = { customer_id: string; display_name: string }
type ApiCallLog = {
  session_id: string; provider: string | null; channel: string | null; direction: string | null
  caller: string | null; callee: string | null; status: string; end_reason: string | null
  started_at: string; answered_at: string | null; ended_at: string | null
  duration_seconds: number | null; transcript_available: boolean; recording_available: boolean
  post_call_status?: string | null; post_call_result?: Record<string, unknown> | null
  post_call_agent_enabled?: boolean
  post_call_error?: string | null
  webhook_status?: string | null
}
type ApiCallDetail = ApiCallLog & { transcript: string | null; recording_allowed: boolean }
type SimulationTemplate = {
  id: string; name: string; apiId: string; destination: string; callerId: string; profileId: string
  liveInstructions: string; liveContext: string; postCallContext: string; outputSchema: string
  tools: string[]; timeZone: string; timeFormat: string; includeLiveContext: boolean; recording: boolean
}
type VoiceAgentProfile = { id: string; name: string; language: string; voice: string; speaking_style: string; instructions: string }
type ApiBillingUsage = {
  configured: boolean; billing_mode?: 'prepaid' | 'postpaid'; currency?: string; rate_per_minute_minor?: number
  calls?: number; billable_seconds?: number; charges_minor?: number; active_reservations?: number
  customer_prepaid_balance_minor?: number; customer_postpaid_used_minor?: number
  customer_postpaid_credit_limit_minor?: number; message?: string
}
type DeleteConfirmation =
  | { kind: 'number'; number: RegisteredNumber }
  | { kind: 'agent'; agent: AgentConfiguration }
  | { kind: 'api'; api: PlatformApiKey }

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
  agentConfigs: () => http.get<{ items: AgentConfiguration[] }>('/v1/admin/customers/agent-configs').then(r => r.data.items),
  apiCalls: (id: string, limit = 50, offset = 0) => http.get<{ items: ApiCallLog[]; total: number }>(`/v1/admin/customers/api-keys/${id}/calls`, { params: { limit, offset } }).then(r => r.data),
  apiCallDetail: (id: string, sessionId: string) => http.get<ApiCallDetail>(`/v1/admin/customers/api-keys/${id}/calls/${sessionId}`).then(r => r.data),
  apiCallRecording: (id: string, sessionId: string) => http.get<Blob>(`/v1/admin/customers/api-keys/${id}/calls/${sessionId}/recording`, { responseType: 'blob' }).then(r => r.data),
  voiceProfiles: (id: string) => http.get<{ items: VoiceAgentProfile[] }>(`/v1/admin/customers/api-keys/${id}/voice-profiles`).then(r => r.data.items),
  simulateCall: (id: string, data: Record<string, unknown>) => http.post<{ session_id: string; status: string }>(`/v1/admin/customers/api-keys/${id}/simulate`, data, { headers: { 'Idempotency-Key': crypto.randomUUID() } }).then(r => r.data),
  apiBilling: (id: string) => http.get<ApiBillingUsage>(`/v1/admin/customers/api-keys/${id}/billing`).then(r => r.data),
  adjustApiBilling: (id: string, mode: 'prepaid' | 'postpaid', amountMinor: number) => http.put(`/v1/admin/customers/api-keys/${id}/billing`, mode === 'prepaid' ? { prepaid_balance_minor: amountMinor } : { postpaid_credit_limit_minor: amountMinor }),
  customers: () => http.get<{ items: Customer[] }>('/v1/admin/customers').then(r => r.data.items),
  addNumbers: (data: { credential_mode: 'platform' | 'custom'; phone_numbers: string[]; standard_channels: number; unlimited_channels: number; tags: string[]; auth_id?: string; auth_token?: string }) => http.post('/v1/admin/customers/phone-numbers', data),
  updateNumber: (number: RegisteredNumber, inbound: boolean, outbound: boolean, standardChannels: number, unlimitedChannels: number, tags: string[]) => http.put(`/v1/admin/customers/phone-numbers/${encodeURIComponent(number.provider)}/${encodeURIComponent(number.phone_number)}`, { inbound_enabled: inbound, outbound_enabled: outbound, standard_channels: standardChannels, unlimited_channels: unlimitedChannels, tags }, { params: { customer_id: number.customer_id } }),
  deleteNumber: (number: RegisteredNumber) => http.delete(`/v1/admin/customers/phone-numbers/${encodeURIComponent(number.provider)}/${encodeURIComponent(number.phone_number)}`, { params: { customer_id: number.customer_id } }),
  createApiKey: (customerName: string, billingMode: 'prepaid' | 'postpaid', billingAmountMinor: number, tags: string[], numbers: RegisteredNumber[], voiceAgentId: string | null, postCallAgentId: string | null, credentials: string, voiceLocation: string, postCallLocation: string, recordingAllowed: boolean, postCallWebhookUrl: string) => http.post<IssuedApiKey>('/v1/admin/customers/api-keys', { customer_name: customerName, billing_mode: billingMode, prepaid_balance_minor: billingMode === 'prepaid' ? billingAmountMinor : 0, postpaid_credit_limit_minor: billingMode === 'postpaid' ? billingAmountMinor : 0, tags, phone_numbers: numbers.map(number => ({ provider: number.provider, phone_number: number.phone_number })), voice_agent_id: voiceAgentId, post_call_agent_id: postCallAgentId, gcp_credentials_json: credentials, voice_location: voiceLocation, post_call_location: postCallLocation, recording_allowed: recordingAllowed, post_call_webhook_url: postCallWebhookUrl.trim() || null }).then(r => r.data),
  updateApiConfig: (id: string, name: string, tags: string[], numbers: RegisteredNumber[], voiceAgentId: string | null, postCallAgentId: string | null, credentials: string, voiceLocation: string, postCallLocation: string, recordingAllowed: boolean) => http.put(`/v1/admin/customers/api-keys/${id}/config`, { name, tags, phone_numbers: numbers.map(number => ({ provider: number.provider, phone_number: number.phone_number })), voice_agent_id: voiceAgentId, post_call_agent_id: postCallAgentId, gcp_credentials_json: credentials || null, voice_location: voiceLocation, post_call_location: postCallLocation, recording_allowed: recordingAllowed }),
  createAgentConfig: (data: { name: string; kind: 'calling' | 'post_call'; provider: 'vertex'; model: string; location: string; cost_per_minute_minor: number; currency: string; tags: string[]; auto_hangup_when_complete: boolean }) => http.post<AgentConfiguration>('/v1/admin/customers/agent-configs', data).then(r => r.data),
  updateAgentConfig: (id: string, data: { name: string; kind: 'calling' | 'post_call'; provider: 'vertex'; model: string; location: string; cost_per_minute_minor: number; currency: string; tags: string[]; auto_hangup_when_complete: boolean }) => http.put<AgentConfiguration>(`/v1/admin/customers/agent-configs/${id}`, data).then(r => r.data),
  deleteAgentConfig: (id: string) => http.delete(`/v1/admin/customers/agent-configs/${id}`),
  deleteApiKey: (id: string) => http.delete(`/v1/admin/customers/api-keys/${id}`),
}

const errorText = (error: unknown) => {
  if (!axios.isAxiosError(error)) return 'Something went wrong. Please try again.'

  const detail: unknown = error.response?.data?.detail
  if (Array.isArray(detail)) {
    const messages = detail.map((issue: unknown) => {
      if (!issue || typeof issue !== 'object') return String(issue)
      const item = issue as { loc?: unknown[]; msg?: unknown }
      const location = Array.isArray(item.loc) ? item.loc.map(String).join('.') : ''
      const message = typeof item.msg === 'string' ? item.msg : 'Invalid value'
      return location ? `${location}: ${message}` : message
    })
    return messages.join('; ') || error.message
  }
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && 'msg' in detail && typeof detail.msg === 'string') {
    return detail.msg
  }
  return error.message
}

export default function App({ mode, onToggleTheme }: { mode: 'light' | 'dark'; onToggleTheme: () => void }) {
  const queryClient = useQueryClient()
  const location = useLocation()
  const navigate = useNavigate()
  const apiDetailId = location.pathname.match(/^\/api\/([^/]+)\/?$/)?.[1] || null
  const page = apiDetailId ? 'API' : pageFromPath(location.pathname)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [profileAnchor, setProfileAnchor] = useState<HTMLElement | null>(null)
  const [apiCallsPage, setApiCallsPage] = useState(0)
  const [apiCallsPageSize, setApiCallsPageSize] = useState(10)
  const [token, setToken] = useState(sessionStorage.getItem('chiefvoice.token') || '')
  const [authReady, setAuthReady] = useState(false)
  const [signingIn, setSigningIn] = useState(false)
  const [authError, setAuthError] = useState('')
  const [assignmentOpen, setAssignmentOpen] = useState(false)
  const [selectedNumbers, setSelectedNumbers] = useState<string[]>([])
  const [apiName, setApiName] = useState('')
  const [apiTags, setApiTags] = useState('')
  const [apiCustomerName, setApiCustomerName] = useState('')
  const [apiWebhookUrl, setApiWebhookUrl] = useState('')
  const [apiBillingMode, setApiBillingMode] = useState<'prepaid' | 'postpaid'>('prepaid')
  const [apiBillingAmount, setApiBillingAmount] = useState('1000')
  const [apiRecordingAllowed, setApiRecordingAllowed] = useState(false)
  const [voiceAgentId, setVoiceAgentId] = useState('')
  const [postCallAgentId, setPostCallAgentId] = useState('')
  const [agentConfigOpen, setAgentConfigOpen] = useState(false)
  const [editingAgentConfig, setEditingAgentConfig] = useState<AgentConfiguration | null>(null)
  const [agentName, setAgentName] = useState('')
  const [agentTags, setAgentTags] = useState('')
  const [agentKind, setAgentKind] = useState<'calling' | 'post_call'>('calling')
  const [agentModel, setAgentModel] = useState('google/gemini-live-2.5-flash-native-audio')
  const [agentLocation, setAgentLocation] = useState('us-central1')
  const [agentCost, setAgentCost] = useState('0.00')
  const [agentCurrency, setAgentCurrency] = useState('INR')
  const [agentAutoHangup, setAgentAutoHangup] = useState(false)
  const [apiCredentials, setApiCredentials] = useState('')
  const [voiceLocation, setVoiceLocation] = useState('us-central1')
  const [postCallLocation, setPostCallLocation] = useState('global')
  const [addNumberAnchor, setAddNumberAnchor] = useState<HTMLElement | null>(null)
  const [addNumberMode, setAddNumberMode] = useState<'platform' | 'custom' | null>(null)
  const [numberInput, setNumberInput] = useState('')
  const [newNumberTags, setNewNumberTags] = useState('')
  const [newStandardChannels, setNewStandardChannels] = useState('1')
  const [newUnlimitedChannels, setNewUnlimitedChannels] = useState('0')
  const [vobizAuthId, setVobizAuthId] = useState('')
  const [vobizAuthToken, setVobizAuthToken] = useState('')
  const [managedNumber, setManagedNumber] = useState<RegisteredNumber | null>(null)
  const [managedInbound, setManagedInbound] = useState(true)
  const [managedOutbound, setManagedOutbound] = useState(true)
  const [managedStandardChannels, setManagedStandardChannels] = useState('1')
  const [managedUnlimitedChannels, setManagedUnlimitedChannels] = useState('0')
  const [managedNumberTags, setManagedNumberTags] = useState('')
  const [managementKey, setManagementKey] = useState<PlatformApiKey | null>(null)
  const [deleteConfirmation, setDeleteConfirmation] = useState<DeleteConfirmation | null>(null)
  const [deleteConfirmationText, setDeleteConfirmationText] = useState('')
  const [simulationApiId, setSimulationApiId] = useState('')
  const [simulationDestination, setSimulationDestination] = useState('')
  const [simulationCallerId, setSimulationCallerId] = useState('')
  const [simulationProfileId, setSimulationProfileId] = useState('')
  const [simulationLiveInstructions, setSimulationLiveInstructions] = useState('')
  const [simulationLiveContext, setSimulationLiveContext] = useState('{}')
  const [simulationPostCallContext, setSimulationPostCallContext] = useState(defaultSimulationPostCallContext)
  const [simulationOutputSchema, setSimulationOutputSchema] = useState(defaultSimulationOutputSchema)
  const [simulationTools, setSimulationTools] = useState<string[]>([])
  const [simulationTimeZone, setSimulationTimeZone] = useState('Asia/Kolkata')
  const [simulationTimeFormat, setSimulationTimeFormat] = useState('%Y-%m-%d %H:%M:%S %z')
  const [simulationIncludeLiveContext, setSimulationIncludeLiveContext] = useState(false)
  const [simulationRecording, setSimulationRecording] = useState(true)
  const [simulationSessionId, setSimulationSessionId] = useState('')
  const [billingAdjustmentOpen, setBillingAdjustmentOpen] = useState(false)
  const [billingAdjustmentAmount, setBillingAdjustmentAmount] = useState('')
  const [oneTimeKey, setOneTimeKey] = useState<IssuedApiKey | null>(null)
  const [toast, setToast] = useState('')
  const connected = Boolean(token)

  useEffect(() => {
    const query = new URLSearchParams(location.search)
    if (query.has('code') || query.has('error')) return
    if (!Object.values(pagePaths).includes(location.pathname.replace(/\/$/, '')) && !apiDetailId) {
      navigate(pagePaths.Numbers, { replace: true })
    }
  }, [location.pathname, location.search, navigate, apiDetailId])

  useEffect(() => {
    setApiCallsPage(0)
  }, [apiDetailId])

  useEffect(() => {
    let active = true
    const initializeAuth = async () => {
      const params = new URLSearchParams(window.location.search)
      const isCallback = params.has('code') || params.has('error')
      try {
        const user = isCallback
          ? await finishLogin()
          : await currentUser()
        if (isCallback) {
          clearLoginCallbackFromUrl()
          navigate(pagePaths.Numbers, { replace: true })
        }
        if (!user || user.expired || !user.access_token) {
          sessionStorage.removeItem('chiefvoice.token')
          if (active) setToken('')
          return
        }
        sessionStorage.setItem('chiefvoice.token', user.access_token)
        if (active) setToken(user.access_token)
      } catch (error) {
        if (isCallback) {
          clearLoginCallbackFromUrl()
          navigate(pagePaths.Numbers, { replace: true })
        }
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
  const keysQuery = useQuery({ queryKey: ['platform-api-keys'], queryFn: api.apiKeys, enabled: connected && (page === 'API' || page === 'Agents' || page === 'Simulation'), retry: false })
  const agentsQuery = useQuery({ queryKey: ['platform-agent-configs'], queryFn: api.agentConfigs, enabled: connected && (page === 'API' || page === 'Agents'), retry: false })
  const simulationProfilesQuery = useQuery({ queryKey: ['simulation-voice-profiles', simulationApiId], queryFn: () => api.voiceProfiles(simulationApiId), enabled: connected && page === 'Simulation' && Boolean(simulationApiId), retry: false })
  const simulationCallsQuery = useQuery({ queryKey: ['simulation-call-logs', simulationApiId], queryFn: () => api.apiCalls(simulationApiId), enabled: connected && page === 'Simulation' && Boolean(simulationApiId), retry: false, refetchInterval: simulationSessionId ? 3000 : false })
  const apiCallsQuery = useQuery({ queryKey: ['platform-api-calls', apiDetailId, apiCallsPage, apiCallsPageSize], queryFn: () => api.apiCalls(apiDetailId!, apiCallsPageSize, apiCallsPage * apiCallsPageSize), enabled: connected && Boolean(apiDetailId), retry: false, refetchInterval: apiDetailId ? 5000 : false })
  const apiBillingQuery = useQuery({ queryKey: ['platform-api-billing', apiDetailId], queryFn: () => api.apiBilling(apiDetailId!), enabled: connected && Boolean(apiDetailId), retry: false, refetchInterval: apiDetailId ? 10000 : false })
  const apiDetailKey = keysQuery.data?.find(key => key.id === apiDetailId) || null
  const issueMutation = useMutation({
    mutationFn: ({ customerName, billingMode, billingAmountMinor, tags, numbers, voiceAgentId, postCallAgentId, credentials, voiceLocation, postCallLocation, recordingAllowed, webhookUrl }: { customerName: string; billingMode: 'prepaid' | 'postpaid'; billingAmountMinor: number; tags: string[]; numbers: RegisteredNumber[]; voiceAgentId: string | null; postCallAgentId: string | null; credentials: string; voiceLocation: string; postCallLocation: string; recordingAllowed: boolean; webhookUrl: string }) => api.createApiKey(customerName, billingMode, billingAmountMinor, tags, numbers, voiceAgentId, postCallAgentId, credentials, voiceLocation, postCallLocation, recordingAllowed, webhookUrl),
    onSuccess: result => {
      void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-agent-configs'] })
      setOneTimeKey(result)
      setAssignmentOpen(false)
      setSelectedNumbers([])
      setApiName(''); setApiTags(''); setApiCustomerName(''); setApiWebhookUrl(''); setApiBillingMode('prepaid'); setApiBillingAmount('1000'); setVoiceAgentId(''); setPostCallAgentId(''); setApiCredentials(''); setApiRecordingAllowed(false)
      setToast('API key created for the selected phone number.')
    },
    onError: error => setToast(errorText(error)),
  })
  const deleteApiKeyMutation = useMutation({
    mutationFn: api.deleteApiKey,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-api-calls'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-api-billing'] })
      setDeleteConfirmation(null); setDeleteConfirmationText('')
      setToast('API and its credentials were permanently deleted from Postgres. Call and billing history remain without an API association.')
    },
    onError: error => setToast(errorText(error)),
  })
  const manageNumbersMutation = useMutation({
    mutationFn: ({ key, name, tags, numbers, voiceAgentId, postCallAgentId, credentials, voiceLocation, postCallLocation, recordingAllowed }: { key: PlatformApiKey; name: string; tags: string[]; numbers: RegisteredNumber[]; voiceAgentId: string | null; postCallAgentId: string | null; credentials: string; voiceLocation: string; postCallLocation: string; recordingAllowed: boolean }) => api.updateApiConfig(key.id, name, tags, numbers, voiceAgentId, postCallAgentId, credentials, voiceLocation, postCallLocation, recordingAllowed),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] })
      setManagementKey(null)
      setSelectedNumbers([])
      setToast('API configuration updated.')
    },
    onError: error => setToast(errorText(error)),
  })
  const adjustBillingMutation = useMutation({
    mutationFn: ({ key, amountMinor, mode }: { key: PlatformApiKey; amountMinor: number; mode: 'prepaid' | 'postpaid' }) => api.adjustApiBilling(key.id, mode, amountMinor),
    onSuccess: (_result, variables) => { void queryClient.invalidateQueries({ queryKey: ['platform-api-billing', variables.key.id] }); setBillingAdjustmentOpen(false); setToast('API billing balance or limit updated.') },
    onError: error => setToast(errorText(error)),
  })
  const saveAgentConfigMutation = useMutation({
    mutationFn: ({ id, data }: { id?: string; data: { name: string; kind: 'calling' | 'post_call'; provider: 'vertex'; model: string; location: string; cost_per_minute_minor: number; currency: string; tags: string[]; auto_hangup_when_complete: boolean } }) => id ? api.updateAgentConfig(id, data) : api.createAgentConfig(data),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['platform-agent-configs'] }); setAgentConfigOpen(false); setEditingAgentConfig(null); setToast('Agent configuration saved.') },
    onError: error => setToast(errorText(error)),
  })
  const deleteAgentConfigMutation = useMutation({
    mutationFn: api.deleteAgentConfig,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-agent-configs'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] })
      setDeleteConfirmation(null); setDeleteConfirmationText('')
      setToast('Agent deleted.')
    },
    onError: error => setToast(errorText(error)),
  })
  const simulateCallMutation = useMutation({
    mutationFn: ({ keyId, data }: { keyId: string; data: Record<string, unknown> }) => api.simulateCall(keyId, data),
    onSuccess: (result, variables) => {
      setSimulationSessionId(result.session_id)
      void queryClient.invalidateQueries({ queryKey: ['simulation-call-logs', variables.keyId] })
      setToast(`Simulation call started · ${result.session_id}`)
    },
    onError: error => setToast(errorText(error)),
  })
  const addNumbersMutation = useMutation({
    mutationFn: api.addNumbers,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-customers'] })
      setAddNumberMode(null); setNumberInput(''); setNewNumberTags(''); setNewStandardChannels('1'); setNewUnlimitedChannels('0'); setVobizAuthId(''); setVobizAuthToken('')
      setToast('Vobiz phone numbers added.')
    },
    onError: error => setToast(errorText(error)),
  })
  const updateNumberMutation = useMutation({
    mutationFn: ({ number, inbound, outbound, standardChannels, unlimitedChannels, tags }: { number: RegisteredNumber; inbound: boolean; outbound: boolean; standardChannels: number; unlimitedChannels: number; tags: string[] }) => api.updateNumber(number, inbound, outbound, standardChannels, unlimitedChannels, tags),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] }); setManagedNumber(null); setToast('Phone number settings updated.') },
    onError: error => setToast(errorText(error)),
  })
  const deleteNumberMutation = useMutation({
    mutationFn: api.deleteNumber,
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] }); void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] }); setDeleteConfirmation(null); setDeleteConfirmationText(''); setManagedNumber(null); setToast('Phone number removed.') },
    onError: error => setToast(errorText(error)),
  })
  const deleteConfirmationLabel = deleteConfirmation?.kind === 'number'
    ? `${deleteConfirmation.number.provider} ${deleteConfirmation.number.phone_number}`
    : deleteConfirmation?.kind === 'agent'
      ? deleteConfirmation.agent.name
      : deleteConfirmation?.kind === 'api'
        ? `${deleteConfirmation.api.customer_name} / ${deleteConfirmation.api.name}`
        : ''
  const deleteConfirmationPhrase = deleteConfirmationLabel ? `DELETE ${deleteConfirmationLabel}` : ''
  const openDeleteConfirmation = (target: DeleteConfirmation) => {
    setDeleteConfirmation(target)
    setDeleteConfirmationText('')
  }
  const confirmDeleteResource = () => {
    if (!deleteConfirmation || deleteConfirmationText !== deleteConfirmationPhrase) return
    if (deleteConfirmation.kind === 'number') deleteNumberMutation.mutate(deleteConfirmation.number)
    if (deleteConfirmation.kind === 'agent') deleteAgentConfigMutation.mutate(deleteConfirmation.agent.id)
    if (deleteConfirmation.kind === 'api') deleteApiKeyMutation.mutate(deleteConfirmation.api.id)
  }
  const deleteConfirmationPending = deleteConfirmation?.kind === 'number'
    ? deleteNumberMutation.isPending
    : deleteConfirmation?.kind === 'agent'
      ? deleteAgentConfigMutation.isPending
      : deleteApiKeyMutation.isPending
  const currentNumbers = numbersQuery.data || []
  const outboundNumbers = currentNumbers
    .filter(number => number.outbound_enabled && !number.api_key_id)
    .filter((number, index, numbers) => numbers.findIndex(candidate => candidate.phone_number === number.phone_number) === index)
  const selected = outboundNumbers.filter(number => selectedNumbers.includes(`${number.provider}|${number.phone_number}`))
  const submitCreateApi = () => {
    if (!selected.length) { setToast('Required: select at least one unassigned outbound phone number.'); return }
    if (!apiCustomerName.trim()) { setToast('Enter the customer name for this API.'); return }
    if (!voiceAgentId && !postCallAgentId) { setToast('Select at least one agent for this API.'); return }
    if (apiWebhookUrl.trim() && !postCallAgentId) { setToast('Select a post-call agent to use a completion webhook.'); return }
    if (!apiCredentials.trim()) { setToast('Add a Google Cloud service account JSON for this API.'); return }
    const configuredRate = (agentsQuery.data || []).filter(agent => agent.id === voiceAgentId || agent.id === postCallAgentId).reduce((sum, agent) => sum + agent.cost_per_minute_minor, 0)
    if (configuredRate < 1) { setToast('Selected agents need a positive combined INR per-minute cost before billing can be enabled.'); return }
    const amount = Number(apiBillingAmount)
    if (!Number.isFinite(amount) || amount <= 0) { setToast('Enter a positive INR starting balance or credit limit.'); return }
    issueMutation.mutate({ customerName: apiCustomerName.trim(), billingMode: apiBillingMode, billingAmountMinor: Math.round(amount * 100), tags: parseTags(apiTags), numbers: selected, voiceAgentId: voiceAgentId || null, postCallAgentId: postCallAgentId || null, credentials: apiCredentials, voiceLocation: voiceLocation.trim(), postCallLocation: postCallLocation.trim(), recordingAllowed: apiRecordingAllowed, webhookUrl: apiWebhookUrl })
  }
  const selectSimulationApi = (id: string) => {
    setSimulationApiId(id)
    setSimulationSessionId('')
    const key = (keysQuery.data || []).find(item => item.id === id)
    setSimulationCallerId(key?.assigned_numbers[0] ? `${key.assigned_numbers[0].provider}|${key.assigned_numbers[0].phone_number}` : '')
    setSimulationProfileId('')
    setSimulationLiveInstructions('')
    setSimulationLiveContext('{}')
    setSimulationPostCallContext(defaultSimulationPostCallContext)
    setSimulationOutputSchema(defaultSimulationOutputSchema)
    setSimulationTools([])
    setSimulationTimeZone('Asia/Kolkata')
    setSimulationTimeFormat('%Y-%m-%d %H:%M:%S %z')
    setSimulationIncludeLiveContext(false)
  }
  const selectSimulationProfile = (id: string) => {
    setSimulationProfileId(id)
    const profile = (simulationProfilesQuery.data || []).find(item => item.id === id)
    setSimulationLiveInstructions(profile?.instructions || '')
    setSimulationLiveContext('{}')
  }
  const startSimulation = (template?: SimulationTemplate) => {
    const config: Omit<SimulationTemplate, 'id' | 'name'> = template || {
      apiId: simulationApiId, destination: simulationDestination, callerId: simulationCallerId,
      profileId: simulationProfileId, liveInstructions: simulationLiveInstructions,
      liveContext: simulationLiveContext, postCallContext: simulationPostCallContext,
      outputSchema: simulationOutputSchema, tools: simulationTools, timeZone: simulationTimeZone,
      timeFormat: simulationTimeFormat, includeLiveContext: simulationIncludeLiveContext,
      recording: simulationRecording,
    }
    const key = (keysQuery.data || []).find(item => item.id === config.apiId)
    if (!key || key.revoked_at) { setToast('Select an active API first.'); return }
    if (!config.destination.trim()) { setToast('Enter the destination phone number.'); return }
    if (!config.callerId) { setToast('Choose an assigned caller ID.'); return }
    if (!key.voice_agent_enabled) { setToast('This API has no calling agent enabled.'); return }
    if (livePromptContainsPostCallDirective(config.liveInstructions)) {
      setToast('These look like post-call instructions. Put transcript extraction or call summaries in the Post-call task field.'); return
    }
    let liveContext: Record<string, unknown>
    let outputSchema: Record<string, unknown>
    try {
      liveContext = JSON.parse(config.liveContext || '{}')
      if (!liveContext || Array.isArray(liveContext) || typeof liveContext !== 'object') throw new Error('Live-call context must be a JSON object.')
      outputSchema = JSON.parse(config.outputSchema)
      if (!outputSchema || Array.isArray(outputSchema) || typeof outputSchema !== 'object') throw new Error('Output schema must be a JSON object.')
    } catch (error) { setToast(error instanceof Error ? error.message : 'Check the JSON fields.'); return }
    const [provider] = config.callerId.split('|')
    const data: Record<string, unknown> = {
      provider, channel: 'phone', destination: config.destination.trim(), caller_id: config.callerId.split('|')[1],
      live_agent: { agent_id: config.profileId || null, instructions: config.liveInstructions, context: liveContext },
      recording_allowed: config.recording,
    }
    if (key.post_call_agent_enabled) data.post_call = {
      context: config.postCallContext.trim(), output_schema: outputSchema, tools: config.tools,
      time_zone: config.timeZone, time_format: config.timeFormat,
      include_live_agent_context: config.includeLiveContext,
    }
    if (template) applySimulationTemplate(template)
    setSimulationSessionId('')
    simulateCallMutation.mutate({ keyId: key.id, data })
  }
  const applySimulationTemplate = (template: SimulationTemplate) => {
    selectSimulationApi(template.apiId)
    setSimulationDestination(template.destination)
    setSimulationCallerId(template.callerId)
    setSimulationProfileId(template.profileId)
    setSimulationLiveInstructions(template.liveInstructions)
    setSimulationLiveContext(template.liveContext)
    setSimulationPostCallContext(template.postCallContext)
    setSimulationOutputSchema(template.outputSchema)
    setSimulationTools(template.tools)
    setSimulationTimeZone(template.timeZone)
    setSimulationTimeFormat(template.timeFormat)
    setSimulationIncludeLiveContext(template.includeLiveContext)
    setSimulationRecording(template.recording)
    setSimulationSessionId('')
  }
  const simulationCall = simulationCallsQuery.data?.items.find(item => item.session_id === simulationSessionId)
  const handleCreateNumberSelection = (value: string[] | string) => {
    const values = typeof value === 'string' ? value.split(',') : value
    setSelectedNumbers(values)
    const availableAgents = agentsQuery.data || []
    if (!availableAgents.some(agent => agent.id === voiceAgentId && agent.kind === 'calling')) setVoiceAgentId(availableAgents.find(agent => agent.kind === 'calling')?.id || '')
    if (!availableAgents.some(agent => agent.id === postCallAgentId && agent.kind === 'post_call')) setPostCallAgentId(availableAgents.find(agent => agent.kind === 'post_call')?.id || '')
    setVoiceLocation(availableAgents.find(agent => agent.id === voiceAgentId)?.location || availableAgents.find(agent => agent.kind === 'calling')?.location || 'us-central1')
    setPostCallLocation(availableAgents.find(agent => agent.id === postCallAgentId)?.location || availableAgents.find(agent => agent.kind === 'post_call')?.location || 'global')
  }
  const openCreateApi = () => {
    setSelectedNumbers([]); setApiName(''); setApiTags(''); setApiCustomerName(''); setApiWebhookUrl(''); setApiBillingMode('prepaid'); setApiBillingAmount('1000'); setVoiceAgentId(''); setPostCallAgentId(''); setApiCredentials(''); setVoiceLocation('us-central1'); setPostCallLocation('global'); setApiRecordingAllowed(false); setAssignmentOpen(true)
  }
  const openEditApi = (key: PlatformApiKey) => {
    setManagementKey(key); setSelectedNumbers(key.assigned_numbers.map(number => `${number.provider}|${number.phone_number}`))
    setApiName(key.name); setApiTags((key.tags || []).join(', ')); setVoiceAgentId(key.voice_agent_id || ''); setPostCallAgentId(key.post_call_agent_id || '')
    setApiCredentials(''); setVoiceLocation(key.voice_location || key.vertex_location || 'us-central1'); setPostCallLocation(key.post_call_location || 'global')
    setApiRecordingAllowed(key.recording_allowed === true)
  }
  const openCreateAgentConfig = () => { setEditingAgentConfig(null); setAgentName(''); setAgentTags(''); setAgentKind('calling'); setAgentModel('google/gemini-live-2.5-flash-native-audio'); setAgentLocation('us-central1'); setAgentCost('0.00'); setAgentCurrency('INR'); setAgentAutoHangup(false); setAgentConfigOpen(true) }
  const openEditAgentConfig = (agent: AgentConfiguration) => { setEditingAgentConfig(agent); setAgentName(agent.name); setAgentTags((agent.tags || []).join(', ')); setAgentKind(agent.kind); setAgentModel(agent.model); setAgentLocation(agent.location); setAgentCost((agent.cost_per_minute_minor / 100).toFixed(2)); setAgentCurrency(agent.currency); setAgentAutoHangup(agent.auto_hangup_when_complete); setAgentConfigOpen(true) }
  const saveAgentConfig = () => {
    if (!agentName.trim() || !agentModel.trim() || !agentLocation.trim()) { setToast('Complete all required agent fields.'); return }
    const cost = Number(agentCost)
    if (!Number.isFinite(cost) || cost < 0) { setToast('Enter a valid non-negative per-minute cost.'); return }
    saveAgentConfigMutation.mutate({ id: editingAgentConfig?.id, data: { name: agentName.trim(), kind: agentKind, provider: 'vertex', model: agentModel.trim(), location: agentLocation.trim(), cost_per_minute_minor: Math.round(cost * 100), currency: agentCurrency.toUpperCase(), tags: parseTags(agentTags), auto_hangup_when_complete: agentKind === 'calling' && agentAutoHangup } })
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
  const navigation = <><div className="brand"><div className="brand-mark"><HubRoundedIcon fontSize="small" /></div><div className="brand-word">chief<span>voice</span></div></div><div className="nav-caption">Platform</div><div className="nav-list">{(['Numbers', 'Agents', 'API', 'Simulation', 'Docs'] as const).map(item => <Button key={item} className={`nav-item ${page === item ? 'active' : ''}`} onClick={() => { navigate(pagePaths[item]); setDrawerOpen(false) }} startIcon={item === 'Numbers' ? <HubRoundedIcon /> : item === 'Agents' ? <AutoAwesomeRoundedIcon /> : item === 'API' ? <KeyRoundedIcon /> : item === 'Simulation' ? <ScienceRoundedIcon /> : <ContentCopyRoundedIcon />}>{item}</Button>)}</div></>

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
          <Tooltip title="Profile menu"><IconButton size="small" onClick={event => setProfileAnchor(event.currentTarget)} aria-label="Open profile menu"><Avatar sx={{ width: 31, height: 31, bgcolor: '#e7eaff', color: '#465bd2', fontSize: 12, fontWeight: 800 }}>CV</Avatar></IconButton></Tooltip>
          <Menu anchorEl={profileAnchor} open={Boolean(profileAnchor)} onClose={() => setProfileAnchor(null)} anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }} transformOrigin={{ vertical: 'top', horizontal: 'right' }}>
            <MenuItem onClick={() => { onToggleTheme(); setProfileAnchor(null) }}>
              {mode === 'light' ? <DarkModeRoundedIcon fontSize="small" sx={{ mr: 1.5 }} /> : <LightModeRoundedIcon fontSize="small" sx={{ mr: 1.5 }} />}
              Switch to {mode === 'light' ? 'dark' : 'light'} theme
            </MenuItem>
            <MenuItem onClick={() => { setProfileAnchor(null); void disconnect() }}>Sign out</MenuItem>
          </Menu>
        </div>
      </header>
      <div className="content">
        {page === 'Numbers' && <NumbersPage connected={connected} onConnect={() => { void connect() }} numbers={currentNumbers} loading={numbersQuery.isLoading} error={numbersQuery.error} onAddNumber={event => setAddNumberAnchor(event.currentTarget)} onManageNumber={number => { setManagedNumber(number); setManagedInbound(number.inbound_enabled); setManagedOutbound(number.outbound_enabled); setManagedStandardChannels(String(number.standard_channels)); setManagedUnlimitedChannels(String(number.unlimited_channels)); setManagedNumberTags((number.tags || []).join(', ')) }} />}
        {page === 'API' && (apiDetailId
          ? <ApiDetailsPage api={apiDetailKey} agents={agentsQuery.data || []} loading={keysQuery.isLoading} error={keysQuery.error} calls={apiCallsQuery.data?.items || []} callsTotal={apiCallsQuery.data?.total || 0} callsPage={apiCallsPage} callsPageSize={apiCallsPageSize} onCallsPageChange={setApiCallsPage} onCallsPageSizeChange={size => { setApiCallsPageSize(size); setApiCallsPage(0) }} callsLoading={apiCallsQuery.isLoading} callsError={apiCallsQuery.error} billing={apiBillingQuery.data} billingLoading={apiBillingQuery.isLoading} billingError={apiBillingQuery.error} onBack={() => navigate(pagePaths.API)} onEdit={key => openEditApi(key)} onAdjustBilling={amount => { setBillingAdjustmentAmount(amount); setBillingAdjustmentOpen(true) }} />
          : <ApiPage connected={connected} numbers={outboundNumbers} keys={keysQuery.data || []} agents={agentsQuery.data || []} loading={keysQuery.isLoading} error={keysQuery.error} onCreate={openCreateApi} onView={key => navigate(`/api/${encodeURIComponent(key.id)}`)} onManage={openEditApi} onDelete={id => { const apiKey = (keysQuery.data || []).find(key => key.id === id); if (apiKey) openDeleteConfirmation({ kind: 'api', api: apiKey }) }} />)}
        {page === 'Agents' && <AgentsPage agents={agentsQuery.data || []} loading={agentsQuery.isLoading} error={agentsQuery.error} onCreate={openCreateAgentConfig} onEdit={openEditAgentConfig} onDelete={agent => openDeleteConfirmation({ kind: 'agent', agent })} />}
        {page === 'Simulation' && <SimulationPage keys={keysQuery.data || []} loading={keysQuery.isLoading} error={keysQuery.error} selectedApiId={simulationApiId} onSelectApi={selectSimulationApi} destination={simulationDestination} onDestinationChange={setSimulationDestination} callerId={simulationCallerId} onCallerIdChange={setSimulationCallerId} profiles={simulationProfilesQuery.data || []} profilesLoading={simulationProfilesQuery.isLoading} profileId={simulationProfileId} onProfileChange={selectSimulationProfile} liveInstructions={simulationLiveInstructions} onLiveInstructionsChange={setSimulationLiveInstructions} liveContext={simulationLiveContext} onLiveContextChange={setSimulationLiveContext} postCallContext={simulationPostCallContext} onPostCallContextChange={setSimulationPostCallContext} outputSchema={simulationOutputSchema} onOutputSchemaChange={setSimulationOutputSchema} tools={simulationTools} onToolsChange={setSimulationTools} timeZone={simulationTimeZone} onTimeZoneChange={setSimulationTimeZone} timeFormat={simulationTimeFormat} onTimeFormatChange={setSimulationTimeFormat} includeLiveContext={simulationIncludeLiveContext} onIncludeLiveContextChange={setSimulationIncludeLiveContext} recording={simulationRecording} onRecordingChange={setSimulationRecording} call={simulationCall} callLoading={simulationCallsQuery.isFetching} onStart={startSimulation} onApplyTemplate={applySimulationTemplate} submitting={simulateCallMutation.isPending} />}
        {page === 'Docs' && <DocsPage />}
      </div>
    </main>

    <Menu anchorEl={addNumberAnchor} open={Boolean(addNumberAnchor)} onClose={() => setAddNumberAnchor(null)}>
      <MenuItem onClick={() => { setAddNumberAnchor(null); setAddNumberMode('platform') }}><ListItemText primary="Add number with platform Vobiz account" secondary="Enter phone numbers; the server uses its configured Vobiz credentials." /></MenuItem>
      <MenuItem onClick={() => { setAddNumberAnchor(null); setAddNumberMode('custom') }}><ListItemText primary="Set up platform Vobiz account" secondary="Enter platform credentials, then add one or more numbers." /></MenuItem>
    </Menu>

    <Dialog open={Boolean(addNumberMode)} onClose={() => setAddNumberMode(null)} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>{addNumberMode === 'custom' ? 'Configure Vobiz numbers' : 'Add phone numbers'}</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        {addNumberMode === 'custom' && <Alert severity="info">Provider selection: <b>Vobiz</b>. These platform credentials apply to this group of platform-owned numbers.</Alert>}
        {addNumberMode === 'platform' && <Alert severity="info">The server’s Vobiz account credentials will be used. Configure <code>VOBIZ_AUTH_ID</code> and <code>VOBIZ_AUTH_TOKEN</code> on the backend.</Alert>}
        {addNumberMode === 'custom' && <>
          <TextField label="Vobiz Auth ID" value={vobizAuthId} onChange={event => setVobizAuthId(event.target.value)} fullWidth autoComplete="off" />
          <TextField label="Vobiz Auth Token" type="password" value={vobizAuthToken} onChange={event => setVobizAuthToken(event.target.value)} fullWidth autoComplete="new-password" />
        </>}
        <Alert severity="info">Configure line capacity per phone number. Standard channels use per-minute carrier pricing; unlimited channels have no carrier line-minute fee. Agent usage remains billed by the API’s agent rate.</Alert>
        <Stack direction={{ xs: 'column', sm: 'row' }} gap={1.5}>
          <TextField label="Standard channels per number" type="number" value={newStandardChannels} onChange={event => setNewStandardChannels(event.target.value)} inputProps={{ min: 0, max: 1000, step: 1 }} fullWidth />
          <TextField label="Unlimited channels per number" type="number" value={newUnlimitedChannels} onChange={event => setNewUnlimitedChannels(event.target.value)} inputProps={{ min: 0, max: 1000, step: 1 }} fullWidth />
        </Stack>
        <TextField label="Phone numbers" value={numberInput} onChange={event => setNumberInput(event.target.value)} multiline minRows={4} fullWidth placeholder={'+15551234567\n+15557654321'} helperText="Enter one number per line in international format. You can add several numbers at once." />
        <TextField label="Tags" value={newNumberTags} onChange={event => setNewNumberTags(event.target.value)} fullWidth placeholder="sales, support, us-east" helperText="Optional. Separate tags with commas." />
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setAddNumberMode(null)}>Cancel</Button><Button variant="contained" disabled={!numberInput.trim() || Number(newStandardChannels) < 0 || Number(newUnlimitedChannels) < 0 || Number(newStandardChannels) + Number(newUnlimitedChannels) < 1 || (addNumberMode === 'custom' && (!vobizAuthId.trim() || !vobizAuthToken.trim())) || addNumbersMutation.isPending} onClick={() => {
        const phoneNumbers = [...new Set(numberInput.split(/[\n,;\s]+/).map(value => value.trim()).filter(Boolean))]
        addNumbersMutation.mutate({ credential_mode: addNumberMode || 'platform', phone_numbers: phoneNumbers, standard_channels: Number(newStandardChannels), unlimited_channels: Number(newUnlimitedChannels), tags: parseTags(newNumberTags), ...(addNumberMode === 'custom' ? { auth_id: vobizAuthId, auth_token: vobizAuthToken } : {}) })
      }}>{addNumbersMutation.isPending ? 'Adding…' : 'Add numbers'}</Button></DialogActions>
    </Dialog>

    <Dialog open={Boolean(managedNumber)} onClose={() => setManagedNumber(null)} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>Manage phone number</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Typography><b>{managedNumber?.phone_number}</b> · {managedNumber?.provider} · Platform inventory</Typography>
        <TextField label="Tags" value={managedNumberTags} onChange={event => setManagedNumberTags(event.target.value)} fullWidth placeholder="sales, support, us-east" helperText="Optional. Separate tags with commas." />
        <FormControl fullWidth><InputLabel id="inbound-number-label">Inbound calls</InputLabel><Select labelId="inbound-number-label" label="Inbound calls" value={managedInbound ? 'enabled' : 'disabled'} onChange={event => setManagedInbound(event.target.value === 'enabled')}><MenuItem value="enabled">Enabled</MenuItem><MenuItem value="disabled">Disabled</MenuItem></Select></FormControl>
        <FormControl fullWidth><InputLabel id="outbound-number-label">Outbound calls</InputLabel><Select labelId="outbound-number-label" label="Outbound calls" value={managedOutbound ? 'enabled' : 'disabled'} onChange={event => setManagedOutbound(event.target.value === 'enabled')}><MenuItem value="enabled">Enabled</MenuItem><MenuItem value="disabled">Disabled</MenuItem></Select></FormControl>
        <Alert severity="info">Unlimited channels do not incur carrier line-minute charges. AI agent minutes are still included in this API customer’s usage.</Alert>
        <Stack direction={{ xs: 'column', sm: 'row' }} gap={1.5}>
          <TextField label="Standard channels" type="number" value={managedStandardChannels} onChange={event => setManagedStandardChannels(event.target.value)} inputProps={{ min: 0, max: 1000, step: 1 }} fullWidth />
          <TextField label="Unlimited channels" type="number" value={managedUnlimitedChannels} onChange={event => setManagedUnlimitedChannels(event.target.value)} inputProps={{ min: 0, max: 1000, step: 1 }} fullWidth />
        </Stack>
        <Typography variant="caption">Credential mode: {managedNumber?.credential_mode || 'Vobiz integration'}</Typography>
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5, justifyContent: 'space-between' }}><Button color="error" disabled={!managedNumber || deleteNumberMutation.isPending} onClick={() => managedNumber && openDeleteConfirmation({ kind: 'number', number: managedNumber })}>Remove number</Button><Stack direction="row" gap={1}><Button onClick={() => setManagedNumber(null)}>Cancel</Button><Button variant="contained" disabled={!managedNumber || (!managedInbound && !managedOutbound) || Number(managedStandardChannels) < 0 || Number(managedUnlimitedChannels) < 0 || Number(managedStandardChannels) + Number(managedUnlimitedChannels) < 1 || updateNumberMutation.isPending} onClick={() => managedNumber && updateNumberMutation.mutate({ number: managedNumber, inbound: managedInbound, outbound: managedOutbound, standardChannels: Number(managedStandardChannels), unlimitedChannels: Number(managedUnlimitedChannels), tags: parseTags(managedNumberTags) })}>{updateNumberMutation.isPending ? 'Saving…' : 'Save settings'}</Button></Stack></DialogActions>
    </Dialog>

    <Dialog open={assignmentOpen} onClose={() => setAssignmentOpen(false)} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>Create API</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="info" sx={{ fontSize: 12 }}>Each API represents a customer. Enter the customer name, assign phone numbers and agent templates, then provide this API's Google Cloud credentials.</Alert>
        <TextField label="Customer name" value={apiCustomerName} onChange={event => setApiCustomerName(event.target.value)} required fullWidth helperText="This will also be the API name." />
        <FormControl fullWidth><InputLabel id="api-billing-mode-label">Billing type</InputLabel><Select labelId="api-billing-mode-label" label="Billing type" value={apiBillingMode} onChange={event => setApiBillingMode(event.target.value as 'prepaid' | 'postpaid')}><MenuItem value="prepaid">Prepaid</MenuItem><MenuItem value="postpaid">Postpaid</MenuItem></Select></FormControl>
        <TextField label={apiBillingMode === 'prepaid' ? 'Starting prepaid balance (INR)' : 'Postpaid credit limit (INR)'} type="number" value={apiBillingAmount} onChange={event => setApiBillingAmount(event.target.value)} inputProps={{ min: 0.01, step: 0.01 }} required fullWidth helperText="This balance or limit is isolated to this API customer." />
        <TextField label="Tags" value={apiTags} onChange={event => setApiTags(event.target.value)} fullWidth placeholder="clinic, priority, us-east" helperText="Optional. Separate tags with commas." />
        <FormControlLabel control={<Switch checked={apiRecordingAllowed} onChange={event => setApiRecordingAllowed(event.target.checked)} />} label="Enable call recordings for this API" />
        <Typography variant="caption" color="text.secondary">When enabled, calls can be recorded according to the recording option sent with each call.</Typography>
        <Typography variant="h6" sx={{ fontWeight: 800 }}>Phone numbers</Typography>
        <FormControl fullWidth disabled={!outboundNumbers.length}>
          <InputLabel id="assigned-number-label">Phone numbers</InputLabel>
          <Select multiple labelId="assigned-number-label" value={selectedNumbers} label="Phone numbers" onChange={event => handleCreateNumberSelection(event.target.value)} renderValue={values => `${values.length} phone number${values.length === 1 ? '' : 's'} selected`}>
            {outboundNumbers.map(number => <MenuItem key={`${number.provider}|${number.phone_number}`} value={`${number.provider}|${number.phone_number}`}><Checkbox checked={selectedNumbers.includes(`${number.provider}|${number.phone_number}`)} size="small" /><ListItemText primary={`${number.phone_number} · ${number.provider}`} secondary="Platform inventory" /></MenuItem>)}
          </Select>
        </FormControl>
        {!outboundNumbers.length && <Alert severity="warning">No unassigned outbound number is available. Add a number in Numbers first; this API can be created once at least one number is selected.</Alert>}
        {(() => { const choices = agentsQuery.data || []; return <>
          <FormControl fullWidth><InputLabel id="create-voice-agent-label">Calling agent</InputLabel><Select labelId="create-voice-agent-label" label="Calling agent" value={voiceAgentId} onChange={event => { const id = event.target.value; setVoiceAgentId(id); const agent = (agentsQuery.data || []).find(item => item.id === id); if (agent) setVoiceLocation(agent.location) }}><MenuItem value="">None</MenuItem>{choices.filter(agent => agent.kind === 'calling').map(agent => <MenuItem key={agent.id} value={agent.id}>{agent.name} · {agent.provider} · {agent.model}</MenuItem>)}</Select></FormControl>
          <FormControl fullWidth><InputLabel id="create-post-agent-label">Post-call agent</InputLabel><Select labelId="create-post-agent-label" label="Post-call agent" value={postCallAgentId} onChange={event => { const id = event.target.value; setPostCallAgentId(id); const agent = (agentsQuery.data || []).find(item => item.id === id); if (agent) setPostCallLocation(agent.location) }}><MenuItem value="">None</MenuItem>{choices.filter(agent => agent.kind === 'post_call').map(agent => <MenuItem key={agent.id} value={agent.id}>{agent.name} · {agent.provider} · {agent.model}</MenuItem>)}</Select></FormControl>
          {voiceAgentId && <TextField label="Calling Vertex location for this API" value={voiceLocation} onChange={event => setVoiceLocation(event.target.value)} fullWidth />}
          {postCallAgentId && <TextField label="Post-call Vertex location for this API" value={postCallLocation} onChange={event => setPostCallLocation(event.target.value)} fullWidth />}
        </> })()}
        {(() => { const rate = (agentsQuery.data || []).filter(agent => agent.id === voiceAgentId || agent.id === postCallAgentId).reduce((sum, agent) => sum + agent.cost_per_minute_minor, 0); return <Alert severity={rate > 0 ? 'info' : 'warning'}>API billing rate: <b>{formatMoney(rate, 'INR')}/minute</b>. It is calculated from the selected calling and post-call agent template costs.</Alert> })()}
        <TextField label="Post-call webhook URL (optional)" type="url" value={apiWebhookUrl} onChange={event => setApiWebhookUrl(event.target.value)} fullWidth placeholder="https://your-service.example.com/webhooks/chiefvoice" helperText="After post-call processing finishes, Chiefvoice sends the result to this HTTPS endpoint. A signing secret is shown once after creation." />
        <TextField label="Google Cloud service account JSON" value={apiCredentials} onChange={event => setApiCredentials(event.target.value)} multiline minRows={5} required fullWidth placeholder="Paste the service account JSON for this API's Vertex project" />
        {!voiceAgentId && postCallAgentId && <Alert severity="warning">This API has only a post-call agent assigned. Outbound phone calls require a calling agent.</Alert>}
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setAssignmentOpen(false)}>Cancel</Button><Button variant="contained" disabled={issueMutation.isPending || !selected.length || !apiCustomerName.trim() || (!voiceAgentId && !postCallAgentId) || !apiCredentials.trim() || Number(apiBillingAmount) <= 0} onClick={submitCreateApi}>{issueMutation.isPending ? 'Creating…' : 'Create API'}</Button></DialogActions>
    </Dialog>

    <Dialog open={!!oneTimeKey} onClose={() => setOneTimeKey(null)} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>Copy the API key</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="warning">The full API key appears once. Save it in the customer’s secret manager before closing this window.</Alert>
        <Typography sx={{ fontSize: 12 }}>Assigned phone numbers: <b>{oneTimeKey?.api_key_record.assigned_numbers.map(number => `${number.phone_number} · ${number.provider}`).join(', ')}</b></Typography>
        <TextField label="API key" value={oneTimeKey?.key || ''} fullWidth multiline minRows={2} InputProps={{ readOnly: true, endAdornment: <InputAdornment position="end"><Tooltip title="Copy key"><IconButton onClick={() => { if (oneTimeKey) void navigator.clipboard.writeText(oneTimeKey.key) }}><ContentCopyRoundedIcon /></IconButton></Tooltip></InputAdornment> }} />
        {oneTimeKey?.post_call_webhook && <><Alert severity="warning">The webhook signing secret is shown once. Store it securely and use it to verify callbacks.</Alert><Typography variant="body2">Webhook endpoint: <b>{oneTimeKey.post_call_webhook.target_url}</b></Typography><TextField label="Webhook signing secret" value={oneTimeKey.post_call_webhook.signing_secret} fullWidth multiline minRows={2} InputProps={{ readOnly: true, endAdornment: <InputAdornment position="end"><Tooltip title="Copy signing secret"><IconButton onClick={() => { if (oneTimeKey?.post_call_webhook) void navigator.clipboard.writeText(oneTimeKey.post_call_webhook.signing_secret) }}><ContentCopyRoundedIcon /></IconButton></Tooltip></InputAdornment> }} /></>}
        {oneTimeKey?.webhook_setup_error && <Alert severity="error">{oneTimeKey.webhook_setup_error}</Alert>}
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button variant="contained" onClick={() => setOneTimeKey(null)}>Done</Button></DialogActions>
    </Dialog>

    <Dialog open={!!managementKey} onClose={() => setManagementKey(null)} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>Edit API configuration</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="info" sx={{ fontSize: 12 }}>Agent model and default location are configured in Agents. This API stores its own Google credentials and may override each agent's location.</Alert>
        <TextField label="API name" value={apiName} onChange={event => setApiName(event.target.value)} required fullWidth />
        <TextField label="Tags" value={apiTags} onChange={event => setApiTags(event.target.value)} fullWidth placeholder="clinic, priority, us-east" helperText="Optional. Separate tags with commas." />
        <FormControlLabel control={<Switch checked={apiRecordingAllowed} onChange={event => setApiRecordingAllowed(event.target.checked)} />} label="Enable call recordings for this API" />
        <Typography variant="caption" color="text.secondary">This authorizes recordings for future calls on the API. Each call must also request recording.</Typography>
        {(() => { const choices = agentsQuery.data || []; return <>
          <FormControl fullWidth><InputLabel id="edit-voice-agent-label">Calling agent</InputLabel><Select labelId="edit-voice-agent-label" label="Calling agent" value={voiceAgentId} onChange={event => { const id = event.target.value; setVoiceAgentId(id); const agent = (agentsQuery.data || []).find(item => item.id === id); if (agent) setVoiceLocation(agent.location) }}><MenuItem value="">None</MenuItem>{choices.filter(agent => agent.kind === 'calling').map(agent => <MenuItem key={agent.id} value={agent.id}>{agent.name} · {agent.provider} · {agent.model}</MenuItem>)}</Select></FormControl>
          <FormControl fullWidth><InputLabel id="edit-post-agent-label">Post-call agent</InputLabel><Select labelId="edit-post-agent-label" label="Post-call agent" value={postCallAgentId} onChange={event => { const id = event.target.value; setPostCallAgentId(id); const agent = (agentsQuery.data || []).find(item => item.id === id); if (agent) setPostCallLocation(agent.location) }}><MenuItem value="">None</MenuItem>{choices.filter(agent => agent.kind === 'post_call').map(agent => <MenuItem key={agent.id} value={agent.id}>{agent.name} · {agent.provider} · {agent.model}</MenuItem>)}</Select></FormControl>
          {voiceAgentId && <TextField label="Calling Vertex location for this API" value={voiceLocation} onChange={event => setVoiceLocation(event.target.value)} fullWidth />}
          {postCallAgentId && <TextField label="Post-call Vertex location for this API" value={postCallLocation} onChange={event => setPostCallLocation(event.target.value)} fullWidth />}
        </> })()}
        <TextField label="Replace Google Cloud service account JSON (optional)" value={apiCredentials} onChange={event => setApiCredentials(event.target.value)} multiline minRows={5} fullWidth placeholder="Saved API credentials are encrypted. Leave blank to keep them." />
        {!voiceAgentId && postCallAgentId && <Alert severity="warning">This API has only a post-call agent assigned. Outbound phone calls require a calling agent.</Alert>}
        <FormControl fullWidth disabled={!managementKey}>
          <InputLabel id="manage-number-label">Phone numbers</InputLabel>
          <Select multiple labelId="manage-number-label" value={selectedNumbers} label="Phone numbers" onChange={event => setSelectedNumbers(typeof event.target.value === 'string' ? event.target.value.split(',') : event.target.value)} renderValue={values => `${values.length} phone number${values.length === 1 ? '' : 's'} selected`}>
            {currentNumbers.filter(number => number.outbound_enabled && (!number.api_key_id || number.api_key_id === managementKey?.id)).filter((number, index, list) => list.findIndex(candidate => candidate.phone_number === number.phone_number) === index).map(number => {
              const value = `${number.provider}|${number.phone_number}`
              return <MenuItem key={value} value={value}><Checkbox checked={selectedNumbers.includes(value)} size="small" /><ListItemText primary={`${number.phone_number} · ${number.provider}`} secondary="Platform inventory" /></MenuItem>
            })}
          </Select>
        </FormControl>
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setManagementKey(null)}>Cancel</Button><Button variant="contained" disabled={!managementKey || !selectedNumbers.length || !apiName.trim() || (!voiceAgentId && !postCallAgentId) || manageNumbersMutation.isPending} onClick={() => { if (!managementKey) return; const numbers = currentNumbers.filter(number => selectedNumbers.includes(`${number.provider}|${number.phone_number}`)); manageNumbersMutation.mutate({ key: managementKey, name: apiName.trim(), tags: parseTags(apiTags), numbers, voiceAgentId: voiceAgentId || null, postCallAgentId: postCallAgentId || null, credentials: apiCredentials, voiceLocation: voiceLocation.trim(), postCallLocation: postCallLocation.trim(), recordingAllowed: apiRecordingAllowed }) }}>{manageNumbersMutation.isPending ? 'Saving…' : 'Save API configuration'}</Button></DialogActions>
    </Dialog>

    <Dialog open={agentConfigOpen} onClose={() => setAgentConfigOpen(false)} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>{editingAgentConfig ? 'Edit agent configuration' : 'Create agent configuration'}</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="info">Agent templates are owned by the platform and can be assigned to customer APIs. Provider, model, default location, and INR cost are managed here; Google Cloud credentials belong to each API.</Alert>
        <TextField label="Agent name" value={agentName} onChange={event => setAgentName(event.target.value)} required fullWidth />
        <TextField label="Tags" value={agentTags} onChange={event => setAgentTags(event.target.value)} fullWidth placeholder="support, multilingual, v2" helperText="Optional. Separate tags with commas." />
        <FormControl fullWidth><InputLabel id="agent-kind-label">Agent type</InputLabel><Select labelId="agent-kind-label" label="Agent type" value={agentKind} onChange={event => { const kind = event.target.value as 'calling' | 'post_call'; setAgentKind(kind); if (!editingAgentConfig) { setAgentModel(kind === 'calling' ? 'google/gemini-live-2.5-flash-native-audio' : 'gemini-3.5-flash-lite'); setAgentLocation(kind === 'calling' ? 'us-central1' : 'global') } }}><MenuItem value="calling">Calling agent</MenuItem><MenuItem value="post_call">Post-call agent</MenuItem></Select></FormControl>
        <FormControl fullWidth><InputLabel id="agent-provider-label">Provider</InputLabel><Select labelId="agent-provider-label" label="Provider" value="vertex" disabled><MenuItem value="vertex">Google Vertex AI</MenuItem></Select></FormControl>
        <TextField label="Model ID" value={agentModel} onChange={event => setAgentModel(event.target.value)} required fullWidth />
        <TextField label="Vertex AI location" value={agentLocation} onChange={event => setAgentLocation(event.target.value)} required fullWidth />
        <Stack direction={{ xs: 'column', sm: 'row' }} gap={1.5}>
          <TextField label="Cost per minute" type="number" value={agentCost} onChange={event => setAgentCost(event.target.value)} inputProps={{ min: 0, step: 0.01 }} helperText="Configured agent cost; informational and separate from the customer billing plan." fullWidth />
          <TextField label="Currency" value="INR" InputProps={{ readOnly: true }} helperText="Cost is recorded in rupees (paise precision)." sx={{ minWidth: 130 }} />
        </Stack>
        {agentKind === 'calling' && <FormControlLabel control={<Switch checked={agentAutoHangup} onChange={event => setAgentAutoHangup(event.target.checked)} />} label="Thank the caller and end the call when the requested information is collected" />}
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setAgentConfigOpen(false)}>Cancel</Button><Button variant="contained" disabled={saveAgentConfigMutation.isPending} onClick={saveAgentConfig}>{saveAgentConfigMutation.isPending ? 'Saving…' : 'Save agent'}</Button></DialogActions>
    </Dialog>

    <Dialog open={billingAdjustmentOpen} onClose={() => setBillingAdjustmentOpen(false)} maxWidth="xs" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>{apiBillingQuery.data?.billing_mode === 'prepaid' ? 'Set prepaid balance' : 'Set postpaid credit limit'}</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="info">This updates billing for {apiDetailKey?.customer_name || 'this API customer'} only. Amounts are in INR.</Alert>
        <TextField autoFocus fullWidth type="number" label={apiBillingQuery.data?.billing_mode === 'prepaid' ? 'Prepaid balance (INR)' : 'Credit limit (INR)'} value={billingAdjustmentAmount} onChange={event => setBillingAdjustmentAmount(event.target.value)} inputProps={{ min: 0, step: 0.01 }} />
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setBillingAdjustmentOpen(false)}>Cancel</Button><Button variant="contained" disabled={adjustBillingMutation.isPending || !billingAdjustmentAmount || Number(billingAdjustmentAmount) < 0 || !Number.isFinite(Number(billingAdjustmentAmount))} onClick={() => { if (!apiDetailKey || !apiBillingQuery.data?.billing_mode) return; adjustBillingMutation.mutate({ key: apiDetailKey, mode: apiBillingQuery.data.billing_mode, amountMinor: Math.round(Number(billingAdjustmentAmount) * 100) }) }}>{adjustBillingMutation.isPending ? 'Saving…' : 'Save billing'}</Button></DialogActions>
    </Dialog>

    <Dialog open={Boolean(deleteConfirmation)} onClose={() => { if (!deleteConfirmationPending) { setDeleteConfirmation(null); setDeleteConfirmationText('') } }} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>Confirm deletion</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="warning">
          {deleteConfirmation?.kind === 'api'
            ? 'This permanently removes the API key, its credentials, and phone-number assignments from Postgres. Call logs and billing ledger entries remain, but their API association will be cleared.'
            : deleteConfirmation?.kind === 'number'
              ? 'This removes the phone number from platform inventory and releases it from its API assignment.'
              : 'This removes the agent from active configuration. An agent assigned to an active API cannot be deleted.'}
        </Alert>
        <Typography variant="body2">Type <code>{deleteConfirmationPhrase}</code> to confirm.</Typography>
        <TextField autoFocus fullWidth label="Confirmation" value={deleteConfirmationText} onChange={event => setDeleteConfirmationText(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && deleteConfirmationText === deleteConfirmationPhrase) confirmDeleteResource() }} />
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}>
        <Button disabled={deleteConfirmationPending} onClick={() => { setDeleteConfirmation(null); setDeleteConfirmationText('') }}>Cancel</Button>
        <Button color="error" variant="contained" disabled={deleteConfirmationText !== deleteConfirmationPhrase || deleteConfirmationPending} onClick={confirmDeleteResource}>{deleteConfirmationPending ? 'Deleting…' : 'Delete'}</Button>
      </DialogActions>
    </Dialog>

    <Snackbar open={Boolean(toast)} autoHideDuration={5000} onClose={() => setToast('')} anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}><Alert onClose={() => setToast('')} severity={toast.toLowerCase().includes('error') || toast.toLowerCase().includes('failed') || toast.toLowerCase().includes('required') ? 'error' : 'success'} variant="filled">{toast}</Alert></Snackbar>
  </div>
}

function NumbersPage({ connected, onConnect, numbers, loading, error, onAddNumber, onManageNumber }: { connected: boolean; onConnect: () => void; numbers: RegisteredNumber[]; loading: boolean; error: unknown; onAddNumber: (event: React.MouseEvent<HTMLButtonElement>) => void; onManageNumber: (number: RegisteredNumber) => void }) {
  return <><PageIntro eyebrow="PHONE INVENTORY" title="Numbers" subtitle="Platform-owned phone inventory across telephony providers." action={<Button variant="contained" startIcon={<AddRoundedIcon />} onClick={onAddNumber} disabled={!connected}>Add number</Button>} />
    <Card className="surface-card" sx={{ mt: 3 }}>
      {!connected ? <ConnectPrompt onConnect={onConnect} /> : loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : numbers.length ? <TableContainer sx={{ width: "100%", overflowX: "auto" }}><Table size="small" sx={{ minWidth: 1220 }}><TableHead><TableRow>{['Phone number', 'Provider', 'Tags', 'Owner', 'Credentials', 'Standard channels', 'Unlimited channels', 'Inbound', 'Outbound', 'API access', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{numbers.map(number => <TableRow key={`${number.provider}|${number.phone_number}|${number.customer_id}`} hover><TableCell className="table-cell" sx={{ fontWeight: 700 }}>{number.phone_number}</TableCell><TableCell className="table-cell"><Chip size="small" label={number.provider} variant="outlined" sx={{ textTransform: 'capitalize' }} /></TableCell><TableCell className="table-cell"><TagChips tags={number.tags || []} /></TableCell><TableCell className="table-cell">Platform</TableCell><TableCell className="table-cell">{number.credential_mode === 'platform' ? 'Platform' : number.credential_mode === 'custom' ? 'Custom account' : '—'}</TableCell><TableCell className="table-cell">{number.standard_channels}</TableCell><TableCell className="table-cell">{number.unlimited_channels}</TableCell><TableCell className="table-cell"><StatusChip enabled={number.inbound_enabled} /></TableCell><TableCell className="table-cell"><StatusChip enabled={number.outbound_enabled} /></TableCell><TableCell className="table-cell">{number.api_key_name || 'Unassigned'}</TableCell><TableCell className="table-cell"><Button size="small" onClick={() => onManageNumber(number)}>Manage</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><HubRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No phone numbers registered</b><div style={{ fontSize: 12, marginTop: 6 }}>Add a number to get started. You can use the platform Vobiz account or add a custom provider account owned by the platform.</div></div>}
    </Card>
  </>
}

function ApiPage({ connected, numbers, keys, agents, loading, error, onCreate, onView, onManage, onDelete }: { connected: boolean; numbers: RegisteredNumber[]; keys: PlatformApiKey[]; agents: AgentConfiguration[]; loading: boolean; error: unknown; onCreate: () => void; onView: (key: PlatformApiKey) => void; onManage: (key: PlatformApiKey) => void; onDelete: (id: string) => void }) {
  return <><PageIntro eyebrow="DEVELOPER ACCESS" title="API" subtitle="Create a phone-number API key and manage existing keys." action={<Button variant="contained" startIcon={<AddRoundedIcon />} onClick={onCreate} disabled={!connected}>Create API</Button>} />
    <Card className="surface-card" sx={{ mt: 3 }}>
      {loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : keys.length ? <TableContainer sx={{ width: "100%", overflowX: "auto" }}><Table size="small" sx={{ minWidth: 900 }}><TableHead><TableRow>{['API name', 'Phone numbers', 'Customer', 'Calling agent', 'Post-call agent', 'Created', 'State', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{keys.map(key => <TableRow key={key.id} hover><TableCell className="table-cell"><b>{key.name}</b><br /><Typography variant="caption"><code>{key.key_prefix}</code></Typography><TagChips tags={key.tags || []} /></TableCell><TableCell className="table-cell" sx={{ fontWeight: 700 }}>{key.assigned_numbers.length ? key.assigned_numbers.map(number => number.phone_number).join(', ') : 'Unassigned'}</TableCell><TableCell className="table-cell">{key.customer_name}</TableCell><TableCell className="table-cell">{agents.find(agent => agent.id === key.voice_agent_id)?.name || 'None'}</TableCell><TableCell className="table-cell">{agents.find(agent => agent.id === key.post_call_agent_id)?.name || 'None'}</TableCell><TableCell className="table-cell">{new Date(key.created_at).toLocaleDateString()}</TableCell><TableCell className="table-cell"><StatusChip enabled={!key.revoked_at} label={key.revoked_at ? 'Revoked' : 'Active'} /></TableCell><TableCell className="table-cell"><Button size="small" onClick={() => onView(key)}>View</Button>{!key.revoked_at && <><Button size="small" onClick={() => onManage(key)}>Edit</Button><Button color="error" size="small" onClick={() => onDelete(key.id)}>Delete</Button></>}</TableCell></TableRow>)}</TableBody></Table></TableContainer> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><KeyRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No APIs created</b><div style={{ fontSize: 12, marginTop: 6 }}>{numbers.length ? 'Create an API by naming it, choosing phone numbers, and assigning configured agents.' : 'Register an outbound-enabled phone number first.'}</div></div>}
    </Card>
  </>
}

function ApiDetailsPage({ api: apiKey, agents, loading, error, calls, callsTotal, callsPage, callsPageSize, onCallsPageChange, onCallsPageSizeChange, callsLoading, callsError, billing, billingLoading, billingError, onBack, onEdit, onAdjustBilling }: {
  api: PlatformApiKey | null; agents: AgentConfiguration[]; loading: boolean; error: unknown
  calls: ApiCallLog[]; callsTotal: number; callsPage: number; callsPageSize: number
  onCallsPageChange: (page: number) => void; onCallsPageSizeChange: (size: number) => void
  callsLoading: boolean; callsError: unknown; billing?: ApiBillingUsage
  billingLoading: boolean; billingError: unknown; onBack: () => void
  onEdit: (key: PlatformApiKey) => void; onAdjustBilling: (amount: string) => void
}) {
  const voiceAgent = agents.find(agent => agent.id === apiKey?.voice_agent_id)
  const postCallAgent = agents.find(agent => agent.id === apiKey?.post_call_agent_id)
  return <>
    <PageIntro eyebrow="API CUSTOMER" title={apiKey?.name || 'API details'} subtitle="Configuration, calls, recordings, transcripts, post-call results, and billing for this API." action={<Stack direction={{ xs: 'column', sm: 'row' }} gap={1}><Button variant="outlined" onClick={onBack}>Back to APIs</Button>{apiKey && !apiKey.revoked_at && <Button variant="contained" onClick={() => onEdit(apiKey)}>Edit API</Button>}</Stack>} />
    {loading ? <Card className="surface-card" sx={{ p: 3 }}><CircularProgress size={24} /></Card> : error ? <Alert severity="error">{errorText(error)}</Alert> : !apiKey ? <Alert severity="warning">This API could not be found. It may have been deleted.</Alert> : <Stack gap={2}>
      <Card className="surface-card" sx={{ p: { xs: 2, md: 3 } }}>
        <Stack gap={2.5}>
          <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" gap={1}>
            <Stack gap={0.75}><Stack direction="row" alignItems="center" gap={1.25} flexWrap="wrap"><Typography variant="h5" sx={{ fontWeight: 800 }}>{apiKey.name}</Typography><StatusChip enabled={!apiKey.revoked_at} label={apiKey.revoked_at ? 'Revoked' : 'Active'} /></Stack><Typography color="text.secondary">Customer: {apiKey.customer_name}</Typography><Stack direction="row" gap={1} alignItems="center" flexWrap="wrap"><Chip size="small" variant="outlined" label={`Key · ${apiKey.key_prefix}`} /><TagChips tags={apiKey.tags || []} /></Stack></Stack>
          </Stack>
          <Stack direction={{ xs: 'column', md: 'row' }} gap={1.5} flexWrap="wrap">
            <UsageCard label="Created" value={new Date(apiKey.created_at).toLocaleString()} />
            <UsageCard label="Assigned numbers" value={apiKey.assigned_numbers.map(number => number.phone_number).join(', ') || 'None'} />
            <UsageCard label="Call recordings" value={apiKey.recording_allowed ? 'Enabled' : 'Disabled'} />
          </Stack>
          <Stack direction={{ xs: 'column', md: 'row' }} gap={1.5} flexWrap="wrap">
            <UsageCard label="Calling agent" value={voiceAgent ? `${voiceAgent.name} · ${voiceAgent.provider}` : 'Not assigned'} />
            <UsageCard label="Calling model" value={apiKey.voice_agent_enabled === false ? 'Disabled' : apiKey.voice_model || voiceAgent?.model || 'Not configured'} />
            <UsageCard label="Calling location" value={apiKey.voice_location || apiKey.vertex_location || voiceAgent?.location || '—'} />
          </Stack>
          <Stack direction={{ xs: 'column', md: 'row' }} gap={1.5} flexWrap="wrap">
            <UsageCard label="Post-call agent" value={postCallAgent ? `${postCallAgent.name} · ${postCallAgent.provider}` : 'Not assigned'} />
            <UsageCard label="Post-call model" value={apiKey.post_call_agent_enabled === false ? 'Disabled' : apiKey.post_call_model || postCallAgent?.model || 'Not configured'} />
            <UsageCard label="Post-call location" value={apiKey.post_call_location || postCallAgent?.location || '—'} />
          </Stack>
        </Stack>
      </Card>

      <Card className="surface-card" sx={{ overflow: 'hidden' }}>
        <Stack direction={{ xs: 'column', sm: 'row' }} alignItems={{ sm: 'center' }} justifyContent="space-between" gap={1} sx={{ px: { xs: 2, md: 3 }, pt: { xs: 2, md: 2.5 }, pb: 2 }}>
          <Stack gap={0.35}><Stack direction="row" alignItems="center" gap={1}><Typography variant="h6" sx={{ fontWeight: 800 }}>Call history</Typography><Chip size="small" label={callsTotal} /></Stack><Typography variant="body2" color="text.secondary">Review call outcomes and open a call to see its recording, transcript, and extracted result.</Typography></Stack>
        </Stack>
        {callsLoading && !calls.length ? <Stack alignItems="center" sx={{ py: 6 }}><CircularProgress size={24} /></Stack> : callsError ? <Alert severity="error" sx={{ mx: 3, mb: 2 }}>{errorText(callsError)}</Alert> : calls.length ? <>
          <Stack gap={1} sx={{ px: { xs: 1.5, md: 2.5 }, pb: 1.5 }}>
            {calls.map(call => <ApiCallRecord key={call.session_id} apiId={apiKey.id} call={call} />)}
          </Stack>
          <TablePagination
            component="div"
            count={callsTotal}
            page={callsPage}
            onPageChange={(_, page) => onCallsPageChange(page)}
            rowsPerPage={callsPageSize}
            onRowsPerPageChange={event => onCallsPageSizeChange(Number(event.target.value))}
            rowsPerPageOptions={[10, 25, 50, 100]}
            labelRowsPerPage="Calls per page"
            showFirstButton
            showLastButton
            sx={{ borderTop: 1, borderColor: 'divider', '.MuiTablePagination-toolbar': { px: { xs: 1.5, md: 2.5 }, flexWrap: 'wrap' } }}
          />
        </> : <Stack alignItems="center" textAlign="center" gap={0.75} sx={{ px: 3, py: 6 }}><Typography sx={{ fontWeight: 700 }}>No calls yet</Typography><Typography variant="body2" color="text.secondary">Use this API from the Docs or Simulation page to place a test call. Calls will appear here when they start.</Typography></Stack>}
      </Card>

      <Card className="surface-card" sx={{ p: { xs: 2, md: 3 } }}>
        <Typography variant="h6" sx={{ fontWeight: 800, mb: 1.5 }}>Billing</Typography>
        {billingLoading ? <CircularProgress size={22} /> : billingError ? <Alert severity="error">{errorText(billingError)}</Alert> : billing?.configured ? <Stack gap={1.5}>
          <Alert severity="info">Billing usage and balance are scoped to this API customer. Amounts are in INR.</Alert>
          <Stack direction={{ xs: 'column', sm: 'row' }} gap={1.5} flexWrap="wrap">
            <UsageCard label="Calls" value={String(billing.calls ?? 0)} />
            <UsageCard label="Billable time" value={`${Math.floor((billing.billable_seconds || 0) / 60)}m ${(billing.billable_seconds || 0) % 60}s`} />
            <UsageCard label="Attributed charges" value={formatMoney(billing.charges_minor || 0, billing.currency || 'INR')} />
            <UsageCard label="Plan / rate" value={`${billing.billing_mode} · ${formatMoney(billing.rate_per_minute_minor || 0, billing.currency || 'INR')}/min`} />
            <UsageCard label="Balance / credit" value={billing.billing_mode === 'prepaid' ? formatMoney(billing.customer_prepaid_balance_minor || 0, billing.currency || 'INR') : `${formatMoney(billing.customer_postpaid_used_minor || 0, billing.currency || 'INR')} used / ${formatMoney(billing.customer_postpaid_credit_limit_minor || 0, billing.currency || 'INR')} limit`} />
          </Stack>
          <Button variant="outlined" sx={{ alignSelf: 'flex-start' }} onClick={() => {
            const minor = billing.billing_mode === 'prepaid' ? billing.customer_prepaid_balance_minor : billing.customer_postpaid_credit_limit_minor
            onAdjustBilling(((minor || 0) / 100).toFixed(2))
          }}>Adjust balance / credit limit</Button>
        </Stack> : <Alert severity="warning">{billing?.message || 'No billing plan is configured for this API customer.'}</Alert>}
      </Card>
    </Stack>}
  </>
}

function ApiCallRecord({ apiId, call }: { apiId: string; call: ApiCallLog }) {
  const [expanded, setExpanded] = useState(false)
  const [requestRecording, setRequestRecording] = useState(false)
  const [recordingUrl, setRecordingUrl] = useState<string | null>(null)
  const detail = useQuery({ queryKey: ['platform-api-call-detail', apiId, call.session_id], queryFn: () => api.apiCallDetail(apiId, call.session_id), enabled: expanded, retry: false, refetchInterval: expanded && (call.status !== 'completed' || call.post_call_status === 'queued' || call.post_call_status === 'running') ? 4000 : false })
  const recording = useQuery({ queryKey: ['platform-api-call-recording', apiId, call.session_id], queryFn: () => api.apiCallRecording(apiId, call.session_id), enabled: expanded && requestRecording && Boolean(detail.data?.recording_available), retry: false })

  useEffect(() => {
    if (!recording.data) return
    const url = URL.createObjectURL(recording.data)
    setRecordingUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [recording.data])

  return <Card variant="outlined" sx={{ p: { xs: 1.5, md: 2 } }}>
    <Stack direction={{ xs: 'column', md: 'row' }} alignItems={{ md: 'center' }} gap={1.5} justifyContent="space-between">
      <Stack gap={0.4}>
        <Typography sx={{ fontWeight: 700 }}>{new Date(call.started_at).toLocaleString()} · {call.direction || 'call'}</Typography>
        <Typography variant="body2" color="text.secondary">{call.caller || '—'} → {call.callee || '—'} · {call.provider || 'unknown provider'} · {call.duration_seconds == null ? 'duration pending' : `${call.duration_seconds}s`}</Typography>
        <Typography variant="caption"><code>{call.session_id}</code></Typography>
      </Stack>
      <Stack direction="row" alignItems="center" gap={1} flexWrap="wrap">
        <StatusChip enabled={call.status === 'completed'} label={call.status} />
        {call.post_call_status && <StatusChip enabled={call.post_call_status === 'completed'} label={`post-call ${call.post_call_status}`} />}
        <Button size="small" variant={expanded ? 'outlined' : 'contained'} onClick={() => setExpanded(value => !value)}>{expanded ? 'Hide details' : 'View details'}</Button>
      </Stack>
    </Stack>
    {expanded && <Stack gap={1.5} sx={{ mt: 2 }}>
      {detail.isLoading ? <CircularProgress size={20} /> : detail.error ? <Alert severity="error">{errorText(detail.error)}</Alert> : detail.data && <>
        <Stack direction={{ xs: 'column', sm: 'row' }} gap={1.5}>
          <Card variant="outlined" sx={{ p: 1.5, flex: 1 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 800, mb: 1 }}>Recording</Typography>
            {detail.data.recording_available ? <Stack gap={1}>
              {recordingUrl ? <audio controls preload="none" src={recordingUrl} style={{ width: '100%' }}>Your browser does not support audio playback.</audio> : <Button size="small" variant="outlined" disabled={recording.isFetching} onClick={() => setRequestRecording(true)}>{recording.isFetching ? 'Loading secure recording…' : 'Load recording'}</Button>}
              {recording.error && <Alert severity="error">{errorText(recording.error)}</Alert>}
            </Stack> : <Typography variant="body2" color="text.secondary">{detail.data.recording_allowed ? 'Recording was allowed, but no audio was saved or it has expired. Check the recording storage logs.' : 'Recording was disabled for this call. Enable recordings for the API and for the call before placing a new one.'}</Typography>}
          </Card>
          <Card variant="outlined" sx={{ p: 1.5, flex: 1 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 800, mb: 1 }}>Post-call output</Typography>
            {detail.data.post_call_status && <StatusChip enabled={detail.data.post_call_status === 'completed'} label={detail.data.post_call_status} />}
            {detail.data.post_call_error && <Alert severity="error" sx={{ mt: 1 }}>{detail.data.post_call_error}</Alert>}
            {detail.data.post_call_result ? <pre style={{ whiteSpace: 'pre-wrap', overflowX: 'auto', maxHeight: 320, padding: 12, borderRadius: 8, background: 'rgba(120,130,150,.12)' }}>{JSON.stringify(detail.data.post_call_result, null, 2)}</pre> : <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>The structured result will appear here when post-call processing completes.</Typography>}
          </Card>
        </Stack>
        <Card variant="outlined" sx={{ p: 1.5 }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 800, mb: 1 }}>Transcript</Typography>
          {detail.data.transcript ? <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 420, overflowY: 'auto', margin: 0, fontFamily: 'inherit' }}>{detail.data.transcript}</pre> : <Typography variant="body2" color="text.secondary">{detail.data.status === 'completed' ? 'Transcript is unavailable for this call.' : 'Transcript will appear here after it is saved.'}</Typography>}
        </Card>
      </>}
    </Stack>}
  </Card>
}

function AgentsPage({ agents, loading, error, onEdit, onCreate, onDelete }: { agents: AgentConfiguration[]; loading: boolean; error: unknown; onEdit: (agent: AgentConfiguration) => void; onCreate: () => void; onDelete: (agent: AgentConfiguration) => void }) {
  return <><PageIntro eyebrow="AGENT CONFIGURATION" title="Agents" subtitle="Create platform-owned calling and post-call templates with provider, model, default location, and cost." action={<Button variant="contained" startIcon={<AddRoundedIcon />} onClick={onCreate}>Create agent</Button>} />
    <Alert severity="info" sx={{ mt: 2 }}>Google Vertex AI is currently available for calling and post-call agents. API credentials and location overrides are configured on each API.</Alert>
    <Card className="surface-card" sx={{ mt: 2 }}>
      {loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : agents.length ? <TableContainer sx={{ width: "100%", overflowX: "auto" }}><Table size="small" sx={{ minWidth: 980 }}><TableHead><TableRow>{['Agent', 'Type', 'Provider / model', 'Default location', 'Cost / minute', 'Auto hang-up', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{agents.map(agent => <TableRow key={agent.id} hover><TableCell className="table-cell"><b>{agent.name}</b><TagChips tags={agent.tags || []} /></TableCell><TableCell className="table-cell">{agent.kind === 'calling' ? 'Calling' : 'Post-call'}</TableCell><TableCell className="table-cell">{agent.provider} · {agent.model}</TableCell><TableCell className="table-cell">{agent.location}</TableCell><TableCell className="table-cell">{formatMoney(agent.cost_per_minute_minor, agent.currency)}/min</TableCell><TableCell className="table-cell">{agent.kind === 'calling' ? <StatusChip enabled={agent.auto_hangup_when_complete} label={agent.auto_hangup_when_complete ? 'enabled' : 'disabled'} /> : '—'}</TableCell><TableCell className="table-cell"><Button size="small" onClick={() => onEdit(agent)}>Edit</Button><Button size="small" color="error" onClick={() => onDelete(agent)}>Delete</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><AutoAwesomeRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No agents configured</b><div style={{ fontSize: 12, marginTop: 6 }}>Create calling or post-call agents first, then assign them to an API.</div><Button variant="contained" sx={{ mt: 2 }} onClick={onCreate}>Create agent</Button></div>}
    </Card>
  </>
}

function SimulationPage(props: {
  keys: PlatformApiKey[]; loading: boolean; error: unknown; selectedApiId: string; onSelectApi: (id: string) => void
  destination: string; onDestinationChange: (value: string) => void; callerId: string; onCallerIdChange: (value: string) => void
  profiles: VoiceAgentProfile[]; profilesLoading: boolean; profileId: string; onProfileChange: (value: string) => void
  liveInstructions: string; onLiveInstructionsChange: (value: string) => void; liveContext: string; onLiveContextChange: (value: string) => void
  postCallContext: string; onPostCallContextChange: (value: string) => void; outputSchema: string; onOutputSchemaChange: (value: string) => void
  tools: string[]; onToolsChange: (value: string[]) => void; timeZone: string; onTimeZoneChange: (value: string) => void
  timeFormat: string; onTimeFormatChange: (value: string) => void; includeLiveContext: boolean; onIncludeLiveContextChange: (value: boolean) => void
  recording: boolean; onRecordingChange: (value: boolean) => void; call?: ApiCallLog; callLoading: boolean
  onStart: (template?: SimulationTemplate) => void
  onApplyTemplate: (template: SimulationTemplate) => void
  submitting: boolean
}) {
  const selected = props.keys.find(key => key.id === props.selectedApiId)
  const assignedNumbers = selected?.assigned_numbers || []
  const [templates, setTemplates] = useState<SimulationTemplate[]>(() => {
    try {
      const parsed: unknown = JSON.parse(window.localStorage.getItem('chiefvoice.simulation-templates') || '[]')
      return Array.isArray(parsed) ? parsed as SimulationTemplate[] : []
    } catch { return [] }
  })
  const [templateDialogOpen, setTemplateDialogOpen] = useState(false)
  const [templateName, setTemplateName] = useState('')
  const [editingTemplateId, setEditingTemplateId] = useState('')
  const [templateNotice, setTemplateNotice] = useState('')
  useEffect(() => {
    try { window.localStorage.setItem('chiefvoice.simulation-templates', JSON.stringify(templates)) } catch { setTemplateNotice('Could not save templates in this browser storage.') }
  }, [templates])
  const canSaveTemplate = Boolean(selected && !selected.revoked_at && selected.voice_agent_enabled && props.destination.trim() && selected.assigned_numbers.some(number => `${number.provider}|${number.phone_number}` === props.callerId))
  const openTemplateDialog = () => {
    if (!canSaveTemplate) { setTemplateNotice('Select an API, caller ID, and destination before saving a template.'); return }
    setEditingTemplateId('')
    setTemplateName('')
    setTemplateDialogOpen(true)
  }
  const saveTemplate = () => {
    const name = templateName.trim()
    if (!name || !selected) return
    const template: SimulationTemplate = {
      id: editingTemplateId || crypto.randomUUID(), name, apiId: props.selectedApiId,
      destination: props.destination.trim(), callerId: props.callerId, profileId: props.profileId,
      liveInstructions: props.liveInstructions, liveContext: props.liveContext,
      postCallContext: props.postCallContext, outputSchema: props.outputSchema, tools: props.tools,
      timeZone: props.timeZone, timeFormat: props.timeFormat,
      includeLiveContext: props.includeLiveContext, recording: props.recording,
    }
    setTemplates(current => editingTemplateId
      ? current.map(item => item.id === editingTemplateId ? template : item)
      : [template, ...current])
    setTemplateNotice(`Template “${name}” saved.`)
    setTemplateDialogOpen(false)
    setEditingTemplateId('')
  }
  const loadTemplateForEdit = (template: SimulationTemplate) => {
    props.onApplyTemplate(template)
    setEditingTemplateId(template.id)
    setTemplateName(template.name)
    setTemplateNotice(`Loaded “${template.name}”. Edit the call fields, then update this template.`)
  }
  const deleteTemplate = (template: SimulationTemplate) => {
    if (!window.confirm(`Delete simulation template “${template.name}”?`)) return
    setTemplates(current => current.filter(item => item.id !== template.id))
    setTemplateNotice(`Template “${template.name}” deleted.`)
    if (editingTemplateId === template.id) setEditingTemplateId('')
  }
  return <>
    <PageIntro eyebrow="DEVELOPER TESTING" title="Simulation" subtitle="Place a real test call through a selected API and inspect its post-call result." />
    <Alert severity="warning" sx={{ mt: 2 }}>This places a real outbound call. The selected API’s billing plan, agent costs, and recording policy apply.</Alert>
    <Card className="surface-card" sx={{ p: { xs: 2, md: 3 }, mt: 2 }}>
      <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" alignItems={{ xs: 'stretch', sm: 'center' }} gap={1.5}>
        <div><Typography variant="h6" sx={{ fontWeight: 800 }}>One-click simulation templates</Typography><Typography variant="body2" color="text.secondary">Save a complete call setup, then send a test call directly from its card. Templates are saved in this browser.</Typography></div>
        <Button variant="outlined" onClick={openTemplateDialog} disabled={!canSaveTemplate}>Save current setup as template</Button>
      </Stack>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>Templates include the destination and context fields. Use placeholders instead of sensitive patient or customer data.</Typography>
      {templateNotice && <Alert severity="info" sx={{ mt: 1.5 }} onClose={() => setTemplateNotice('')}>{templateNotice}</Alert>}
      {templates.length ? <Stack gap={1.25} sx={{ mt: 2 }}>{templates.map(template => {
        const templateApi = props.keys.find(key => key.id === template.apiId)
        const isUsable = Boolean(templateApi && !templateApi.revoked_at && templateApi.voice_agent_enabled && templateApi.assigned_numbers.some(number => `${number.provider}|${number.phone_number}` === template.callerId))
        return <Card key={template.id} variant="outlined" sx={{ p: 1.5 }}><Stack direction={{ xs: 'column', md: 'row' }} justifyContent="space-between" alignItems={{ xs: 'stretch', md: 'center' }} gap={1.5}>
          <div><Typography sx={{ fontWeight: 800 }}>{template.name}</Typography><Typography variant="caption" color="text.secondary">{templateApi ? `${templateApi.customer_name} · ${templateApi.name}` : 'API unavailable'} · {template.destination} · {templateApi?.assigned_numbers.find(number => `${number.provider}|${number.phone_number}` === template.callerId)?.phone_number || 'Caller ID unavailable'}</Typography></div>
          <Stack direction="row" gap={0.5} flexWrap="wrap"><Button size="small" onClick={() => loadTemplateForEdit(template)}>Load / edit</Button><Button size="small" color="error" onClick={() => deleteTemplate(template)}>Delete</Button><Button size="small" variant="contained" startIcon={<ScienceRoundedIcon />} disabled={!isUsable || props.submitting} onClick={() => props.onStart(template)}>{props.submitting ? 'Sending…' : 'Send test call'}</Button></Stack>
        </Stack></Card>
      })}</Stack> : <div className="empty-state" style={{ marginTop: 16 }}>No saved templates. Fill in the call setup below and save it for one-click testing.</div>}
    </Card>
    <Card className="surface-card" sx={{ p: { xs: 2, md: 3 }, mt: 2 }}>
      {props.loading ? <CircularProgress size={24} /> : props.error ? <Alert severity="error">{errorText(props.error)}</Alert> : !props.keys.length ? <div className="empty-state">Create an API and assign a phone number before starting a simulation.</div> : <Stack gap={2.5}>
        <Typography variant="h6" sx={{ fontWeight: 800 }}>Call setup</Typography>
        <FormControl fullWidth><InputLabel id="simulation-api-label">API customer</InputLabel><Select labelId="simulation-api-label" label="API customer" value={props.selectedApiId} onChange={event => props.onSelectApi(event.target.value)}>
          {props.keys.filter(key => !key.revoked_at).map(key => <MenuItem key={key.id} value={key.id}>{key.customer_name} · {key.name} · {key.key_prefix}</MenuItem>)}
        </Select></FormControl>
        {selected && <Alert severity="info">Calling model: <b>{selected.voice_model || 'Not configured'}</b> · Post-call model: <b>{selected.post_call_agent_enabled ? selected.post_call_model || 'Not configured' : 'disabled'}</b>. This test uses this API’s credentials, assigned phone number, and customer billing plan.</Alert>}
        <Stack direction={{ xs: 'column', sm: 'row' }} gap={1.5}>
          <TextField fullWidth label="Destination phone number" value={props.destination} onChange={event => props.onDestinationChange(event.target.value)} placeholder="+919876543210" />
          <FormControl fullWidth disabled={!assignedNumbers.length}><InputLabel id="simulation-caller-label">Assigned caller ID</InputLabel><Select labelId="simulation-caller-label" label="Assigned caller ID" value={props.callerId} onChange={event => props.onCallerIdChange(event.target.value)}>{assignedNumbers.map(number => <MenuItem key={`${number.provider}|${number.phone_number}`} value={`${number.provider}|${number.phone_number}`}>{number.phone_number} · {number.provider}</MenuItem>)}</Select></FormControl>
        </Stack>
        {!assignedNumbers.length && selected && <Alert severity="error">This API has no assigned phone number. Assign a number in API settings first.</Alert>}
        <Typography variant="h6" sx={{ fontWeight: 800, mt: 1 }}>Live-call agent</Typography>
        <FormControl fullWidth disabled={!selected}><InputLabel id="simulation-profile-label">Agent profile (optional)</InputLabel><Select labelId="simulation-profile-label" label="Agent profile (optional)" value={props.profileId} onChange={event => props.onProfileChange(event.target.value)}><MenuItem value="">API default voice</MenuItem>{props.profiles.map(profile => <MenuItem key={profile.id} value={profile.id}>{profile.name} · {profile.language} · {profile.voice}</MenuItem>)}</Select></FormControl>
        {props.profilesLoading && <Typography variant="caption" color="text.secondary">Loading profiles owned by this API customer…</Typography>}
        <TextField fullWidth multiline minRows={4} label="Live-call prompt / instructions" value={props.liveInstructions} onChange={event => props.onLiveInstructionsChange(event.target.value)} placeholder="Tell the caller who the agent is, the goal of the call, and how it should handle questions." helperText="Give spoken-conversation directions here. Put transcript extraction, summaries, and result formatting in the Post-call task below." />
        <TextField fullWidth multiline minRows={3} label="Live-call context (JSON object)" value={props.liveContext} onChange={event => props.onLiveContextChange(event.target.value)} helperText="Per-call facts for personalization. Instructions belong in the prompt above." />
        <Typography variant="h6" sx={{ fontWeight: 800, mt: 1 }}>Post-call agent</Typography>
        {!selected?.post_call_agent_enabled ? <Alert severity="info">Post-call analysis is disabled for this API. Enable a post-call agent in API settings to test extraction.</Alert> : <>
          <TextField fullWidth multiline minRows={3} label="Post-call task / prompt" value={props.postCallContext} onChange={event => props.onPostCallContextChange(event.target.value)} />
          <TextField fullWidth multiline minRows={8} label="Expected output schema (JSON)" value={props.outputSchema} onChange={event => props.onOutputSchemaChange(event.target.value)} helperText="The result is validated against this schema before delivery." />
          <Stack direction={{ xs: 'column', sm: 'row' }} gap={1.5}>
            <TextField fullWidth label="Time zone (IANA)" value={props.timeZone} onChange={event => props.onTimeZoneChange(event.target.value)} placeholder="Asia/Kolkata" />
            <TextField fullWidth label="Time format (strftime; include %z)" value={props.timeFormat} onChange={event => props.onTimeFormatChange(event.target.value)} />
          </Stack>
          <Stack direction={{ xs: 'column', sm: 'row' }} gap={1}>
            <Chip clickable color={props.tools.includes('calculator') ? 'primary' : 'default'} variant={props.tools.includes('calculator') ? 'filled' : 'outlined'} label="Calculator tool" onClick={() => props.onToolsChange(props.tools.includes('calculator') ? props.tools.filter(tool => tool !== 'calculator') : [...props.tools, 'calculator'])} />
            <Chip clickable color={props.tools.includes('time') ? 'primary' : 'default'} variant={props.tools.includes('time') ? 'filled' : 'outlined'} label="Time tool" onClick={() => props.onToolsChange(props.tools.includes('time') ? props.tools.filter(tool => tool !== 'time') : [...props.tools, 'time'])} />
          </Stack>
          <FormControl><Stack direction="row" alignItems="center" gap={1}><Checkbox checked={props.includeLiveContext} onChange={event => props.onIncludeLiveContextChange(event.target.checked)} /><Typography variant="body2">Include the live-call context in post-call analysis</Typography></Stack></FormControl>
        </>}
        {!selected?.recording_allowed && <Alert severity="warning">Recordings are disabled for this API. Enable them in the API’s Edit configuration before starting a call you want recorded.</Alert>}
        <FormControl><Stack direction="row" alignItems="center" gap={1}><Checkbox checked={props.recording} disabled={!selected?.recording_allowed} onChange={event => props.onRecordingChange(event.target.checked)} /><Typography variant="body2">Record this test call</Typography></Stack></FormControl>
        <Stack direction={{ xs: 'column', sm: 'row' }} gap={1}>
          {editingTemplateId && <Button variant="outlined" onClick={() => setTemplateDialogOpen(true)} disabled={!canSaveTemplate}>Update current template</Button>}
          <Button variant="contained" startIcon={<ScienceRoundedIcon />} onClick={() => props.onStart()} disabled={!selected || !selected.voice_agent_enabled || !props.callerId || !props.destination.trim() || props.submitting}>{props.submitting ? 'Starting call…' : 'Start test call'}</Button>
        </Stack>
      </Stack>}
    </Card>
    {props.call && <Card className="surface-card" sx={{ p: { xs: 2, md: 3 }, mt: 2 }}>
      <Stack gap={1.5}><Stack direction="row" alignItems="center" gap={1}><Typography variant="h6" sx={{ fontWeight: 800 }}>Simulation result</Typography><StatusChip enabled={props.call.status === 'completed'} label={props.call.status} />{props.callLoading && <CircularProgress size={16} />}</Stack>
        <Typography variant="body2">Session <code>{props.call.session_id}</code> · {props.call.caller} → {props.call.callee}</Typography>
        <Stack direction="row" gap={1} alignItems="center"><Typography variant="body2">Post-call</Typography>{props.call.post_call_status ? <StatusChip enabled={props.call.post_call_status === 'completed'} label={props.call.post_call_status} /> : props.call.post_call_agent_enabled === false ? <StatusChip enabled={false} label="disabled" /> : <StatusChip enabled={false} label="waiting" />}</Stack>
        {props.call.post_call_result && <><Typography variant="subtitle2">Validated extraction</Typography><pre style={{ whiteSpace: 'pre-wrap', overflowX: 'auto', padding: 16, borderRadius: 10, background: 'rgba(120,130,150,.12)' }}>{JSON.stringify(props.call.post_call_result, null, 2)}</pre></>}
        {props.call.end_reason && <Alert severity={props.call.status === 'completed' ? 'info' : 'error'}>Call ended: {props.call.end_reason}</Alert>}
      </Stack>
    </Card>}
    <Dialog open={templateDialogOpen} onClose={() => setTemplateDialogOpen(false)} maxWidth="xs" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>{editingTemplateId ? 'Update simulation template' : 'Save simulation template'}</DialogTitle>
      <DialogContent><TextField autoFocus label="Template name" value={templateName} onChange={event => setTemplateName(event.target.value)} required fullWidth sx={{ mt: 1 }} /></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setTemplateDialogOpen(false)}>Cancel</Button><Button variant="contained" disabled={!templateName.trim() || !canSaveTemplate} onClick={saveTemplate}>{editingTemplateId ? 'Update template' : 'Save template'}</Button></DialogActions>
    </Dialog>
  </>
}

function ConnectPrompt({ onConnect }: { onConnect: () => void }) { return <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><KeyRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>Sign in to manage the platform</b><div style={{ fontSize: 12, marginTop: 6 }}>Use your allowlisted platform administrator identity.</div><Button sx={{ mt: 1.5 }} variant="contained" onClick={onConnect}>Sign in with OIDC</Button></div> }
function UsageCard({ label, value }: { label: string; value: string }) { return <Card variant="outlined" sx={{ p: 1.5, flex: 1, minWidth: 150 }}><Typography variant="caption" color="text.secondary">{label}</Typography><Typography sx={{ fontWeight: 800, mt: 0.4 }}>{value}</Typography></Card> }
function formatMoney(amountMinor: number, currency: string) { return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(amountMinor / 100) }
function parseTags(value: string) { return [...new Set(value.split(',').map(tag => tag.trim().toLowerCase()).filter(Boolean))] }
function TagChips({ tags }: { tags: string[] }) { return tags.length ? <Stack direction="row" gap={0.5} flexWrap="wrap" sx={{ mt: 0.5 }}>{tags.map(tag => <Chip size="small" key={tag} label={tag} variant="outlined" />)}</Stack> : null }
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
    "recording_allowed": true,
    "live_agent": {
      "agent_id": "agent-profile-id",
      "instructions": "Confirm the appointment politely. Do not make up availability.",
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
  const statusExample = `curl "$CHIEFVOICE_URL/v1/calls/$SESSION_ID" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY"`
  const postCallExample = `curl "$CHIEFVOICE_URL/v1/calls/$SESSION_ID/post-call" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY"`
  const recordingExample = `curl "$CHIEFVOICE_URL/v1/calls/$SESSION_ID/recording" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY" \\
  --output recording.wav`
  const retryAnalysisExample = `curl -X POST "$CHIEFVOICE_URL/v1/calls/$SESSION_ID/post-call/retry" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY"`
  const configureWebhookExample = `curl -X PUT "$CHIEFVOICE_URL/v1/webhooks/post-call" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"target_url":"https://your-service.example.com/webhooks/chiefvoice"}'`
  const webhookManagementExample = `# View the current webhook URL\ncurl "$CHIEFVOICE_URL/v1/webhooks/post-call" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY"\n\n# Remove the webhook\ncurl -X DELETE "$CHIEFVOICE_URL/v1/webhooks/post-call" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY"`
  const retryWebhookExample = `curl -X POST "$CHIEFVOICE_URL/v1/webhooks/post-call/deliveries/$SESSION_ID/retry" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY"`
  return <><PageIntro eyebrow="DEVELOPER GUIDE" title="API documentation" subtitle="Start a call, check its progress, and receive structured post-call results." />
    <Stack gap={2} sx={{ mt: 3 }}>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Authentication and phone numbers</Typography><Typography sx={{ mt: 1 }}>Use the full API key shown once when the API was created. Send it as a Bearer token. It can only call from phone numbers assigned to that API. If you assigned multiple numbers for a provider, include <code>caller_id</code>.</Typography><Typography sx={{ mt: 1 }}>Create the API key in the API page. Credentials and phone number assignments are editable from the API’s detail view.</Typography></Card>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Start an outbound call</Typography><Typography sx={{ mt: 1, mb: 1.5 }}>The call request returns an accepted session ID quickly; it does not hold the HTTP connection open while the conversation runs. Use the Simulation page to run the same flow from the console with an API, prompts, and extraction schema.</Typography><CopyableCode value={callBody} minRows={21} /></Card>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Useful call endpoints</Typography><Typography sx={{ mt: 1, mb: 2 }}>Replace <code>$SESSION_ID</code> with the session ID returned when the call starts. Each example includes a copy button.</Typography>
        <Stack gap={2}><DocCodeBlock title="Check call status" value={statusExample} /><DocCodeBlock title="Get post-call status and result" value={postCallExample} /><DocCodeBlock title="Download the recording" value={recordingExample} /><DocCodeBlock title="Retry post-call analysis after a failure" value={retryAnalysisExample} /></Stack>
      </Card>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Context, output schema, and tools</Typography><Typography sx={{ mt: 1 }}>Keep live conversation context in <code>live_agent.context</code>. Put extraction instructions and the JSON output schema in <code>post_call</code>. The schema is validated with strict types before the result is stored or delivered. Supported post-call tools are <code>calculator</code> and <code>time</code>; when using the time tool, set an IANA timezone and a format containing <code>%z</code>.</Typography><Typography sx={{ mt: 1 }}>Use <code>include_live_agent_context</code> only when post-call extraction needs the live context too. Treat supplied context as reference data and provide an “unknown” option for facts the agent cannot establish.</Typography></Card>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Configure post-call webhooks</Typography><Typography sx={{ mt: 1, mb: 2 }}>The CRM or customer service exposes the receiving URL. Chiefvoice sends a signed callback after post-call analysis finishes. Return a 2xx response when received, and deduplicate retries by event ID. Polling remains available as a fallback.</Typography>
        <Alert severity="info" sx={{ mb: 2 }}>Webhook routes require <code>webhooks:read</code> and <code>webhooks:write</code>. New APIs created from the console receive these scopes. API keys created before webhook scopes were added will continue to return 403 on these routes.</Alert>
        <Stack gap={2}><DocCodeBlock title="Register or update the callback URL" value={configureWebhookExample} /><DocCodeBlock title="View or remove the callback URL" value={webhookManagementExample} /><DocCodeBlock title="Retry a failed webhook delivery" value={retryWebhookExample} /></Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1.5 }}>The signing secret is returned once when you configure the webhook. Store it securely to verify callback signatures.</Typography>
      </Card>
    </Stack>
  </>
}
function CopyableCode({ value, minRows = 2 }: { value: string; minRows?: number }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    void navigator.clipboard.writeText(value).then(() => setCopied(true)).catch(() => setCopied(false))
  }
  return <TextField value={value} fullWidth multiline minRows={minRows} InputProps={{ readOnly: true, endAdornment: <InputAdornment position="end"><Tooltip title={copied ? 'Copied' : 'Copy'}><IconButton aria-label={copied ? 'Copied' : 'Copy code'} onClick={copy}>{copied ? <CheckCircleRoundedIcon color="success" /> : <ContentCopyRoundedIcon />}</IconButton></Tooltip></InputAdornment> }} />
}
function DocCodeBlock({ title, value }: { title: string; value: string }) {
  return <Stack gap={0.75}><Typography variant="subtitle2" sx={{ fontWeight: 700 }}>{title}</Typography><CopyableCode value={value} minRows={value.split('\n').length} /></Stack>
}
function StatusChip({ enabled, label }: { enabled: boolean; label?: string }) { return <Chip size="small" label={label || (enabled ? 'Enabled' : 'Disabled')} color={enabled ? 'success' : 'default'} variant={enabled ? 'filled' : 'outlined'} sx={{ fontSize: 10, height: 22 }} /> }
function PageIntro({ eyebrow, title, subtitle, action }: { eyebrow: string; title: string; subtitle: string; action?: React.ReactNode }) { return <div style={{ display: 'flex', alignItems: 'end', justifyContent: 'space-between', gap: 18, flexWrap: 'wrap' }}><div><div className="eyebrow">{eyebrow}</div><Typography className="page-title" variant="h4">{title}</Typography><div className="page-subtitle">{subtitle}</div></div>{action}</div> }
