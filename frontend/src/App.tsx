import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import axios from 'axios'
import {
  Alert, Avatar, Button, Card, Checkbox, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, Drawer, FormControl, IconButton, InputAdornment,
  InputLabel, ListItemText, Menu, MenuItem, Select, Snackbar, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TableContainer, TextField, Tooltip, Typography,
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
  tags: string[]
}
type AgentConfiguration = {
  id: string; owner: 'platform'; name: string
  kind: 'calling' | 'post_call'; provider: string; model: string; location: string
  cost_per_minute_minor: number; currency: string
  enabled: boolean
  tags: string[]
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
  apiCalls: (id: string) => http.get<{ items: ApiCallLog[]; total: number }>(`/v1/admin/customers/api-keys/${id}/calls`).then(r => r.data),
  voiceProfiles: (id: string) => http.get<{ items: VoiceAgentProfile[] }>(`/v1/admin/customers/api-keys/${id}/voice-profiles`).then(r => r.data.items),
  simulateCall: (id: string, data: Record<string, unknown>) => http.post<{ session_id: string; status: string }>(`/v1/admin/customers/api-keys/${id}/simulate`, data, { headers: { 'Idempotency-Key': crypto.randomUUID() } }).then(r => r.data),
  apiBilling: (id: string) => http.get<ApiBillingUsage>(`/v1/admin/customers/api-keys/${id}/billing`).then(r => r.data),
  adjustApiBilling: (id: string, mode: 'prepaid' | 'postpaid', amountMinor: number) => http.put(`/v1/admin/customers/api-keys/${id}/billing`, mode === 'prepaid' ? { prepaid_balance_minor: amountMinor } : { postpaid_credit_limit_minor: amountMinor }),
  customers: () => http.get<{ items: Customer[] }>('/v1/admin/customers').then(r => r.data.items),
  addNumbers: (data: { credential_mode: 'platform' | 'custom'; phone_numbers: string[]; standard_channels: number; unlimited_channels: number; tags: string[]; auth_id?: string; auth_token?: string }) => http.post('/v1/admin/customers/phone-numbers', data),
  updateNumber: (number: RegisteredNumber, inbound: boolean, outbound: boolean, standardChannels: number, unlimitedChannels: number, tags: string[]) => http.put(`/v1/admin/customers/phone-numbers/${encodeURIComponent(number.provider)}/${encodeURIComponent(number.phone_number)}`, { inbound_enabled: inbound, outbound_enabled: outbound, standard_channels: standardChannels, unlimited_channels: unlimitedChannels, tags }, { params: { customer_id: number.customer_id } }),
  deleteNumber: (number: RegisteredNumber) => http.delete(`/v1/admin/customers/phone-numbers/${encodeURIComponent(number.provider)}/${encodeURIComponent(number.phone_number)}`, { params: { customer_id: number.customer_id } }),
  createApiKey: (customerName: string, billingMode: 'prepaid' | 'postpaid', billingAmountMinor: number, name: string, tags: string[], numbers: RegisteredNumber[], voiceAgentId: string | null, postCallAgentId: string | null, credentials: string, voiceLocation: string, postCallLocation: string) => http.post<IssuedApiKey>('/v1/admin/customers/api-keys', { customer_name: customerName, billing_mode: billingMode, prepaid_balance_minor: billingMode === 'prepaid' ? billingAmountMinor : 0, postpaid_credit_limit_minor: billingMode === 'postpaid' ? billingAmountMinor : 0, name, tags, phone_numbers: numbers.map(number => ({ provider: number.provider, phone_number: number.phone_number })), voice_agent_id: voiceAgentId, post_call_agent_id: postCallAgentId, gcp_credentials_json: credentials, voice_location: voiceLocation, post_call_location: postCallLocation }).then(r => r.data),
  updateApiConfig: (id: string, name: string, tags: string[], numbers: RegisteredNumber[], voiceAgentId: string | null, postCallAgentId: string | null, credentials: string, voiceLocation: string, postCallLocation: string) => http.put(`/v1/admin/customers/api-keys/${id}/config`, { name, tags, phone_numbers: numbers.map(number => ({ provider: number.provider, phone_number: number.phone_number })), voice_agent_id: voiceAgentId, post_call_agent_id: postCallAgentId, gcp_credentials_json: credentials || null, voice_location: voiceLocation, post_call_location: postCallLocation }),
  createAgentConfig: (data: { name: string; kind: 'calling' | 'post_call'; provider: 'vertex'; model: string; location: string; cost_per_minute_minor: number; currency: string; tags: string[] }) => http.post<AgentConfiguration>('/v1/admin/customers/agent-configs', data).then(r => r.data),
  updateAgentConfig: (id: string, data: { name: string; kind: 'calling' | 'post_call'; provider: 'vertex'; model: string; location: string; cost_per_minute_minor: number; currency: string; tags: string[] }) => http.put<AgentConfiguration>(`/v1/admin/customers/agent-configs/${id}`, data).then(r => r.data),
  deleteAgentConfig: (id: string) => http.delete(`/v1/admin/customers/agent-configs/${id}`),
  deleteApiKey: (id: string) => http.delete(`/v1/admin/customers/api-keys/${id}`),
}

const errorText = (error: unknown) => axios.isAxiosError(error)
  ? String(error.response?.data?.detail || error.message)
  : 'Something went wrong. Please try again.'

export default function App({ mode, onToggleTheme }: { mode: 'light' | 'dark'; onToggleTheme: () => void }) {
  const queryClient = useQueryClient()
  const location = useLocation()
  const navigate = useNavigate()
  const page = pageFromPath(location.pathname)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [profileAnchor, setProfileAnchor] = useState<HTMLElement | null>(null)
  const [token, setToken] = useState(sessionStorage.getItem('chiefvoice.token') || '')
  const [authReady, setAuthReady] = useState(false)
  const [signingIn, setSigningIn] = useState(false)
  const [authError, setAuthError] = useState('')
  const [assignmentOpen, setAssignmentOpen] = useState(false)
  const [selectedNumbers, setSelectedNumbers] = useState<string[]>([])
  const [apiName, setApiName] = useState('')
  const [apiTags, setApiTags] = useState('')
  const [apiCustomerName, setApiCustomerName] = useState('')
  const [apiBillingMode, setApiBillingMode] = useState<'prepaid' | 'postpaid'>('prepaid')
  const [apiBillingAmount, setApiBillingAmount] = useState('1000')
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
  const [apiCredentials, setApiCredentials] = useState('')
  const [voiceLocation, setVoiceLocation] = useState('us-central1')
  const [postCallLocation, setPostCallLocation] = useState('us-central1')
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
  const [viewingKey, setViewingKey] = useState<PlatformApiKey | null>(null)
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
    if (!Object.values(pagePaths).includes(location.pathname.replace(/\/$/, ''))) {
      navigate(pagePaths.Numbers, { replace: true })
    }
  }, [location.pathname, location.search, navigate])

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
  const apiCallsQuery = useQuery({ queryKey: ['platform-api-calls', viewingKey?.id], queryFn: () => api.apiCalls(viewingKey!.id), enabled: connected && Boolean(viewingKey), retry: false, refetchInterval: viewingKey ? 5000 : false })
  const apiBillingQuery = useQuery({ queryKey: ['platform-api-billing', viewingKey?.id], queryFn: () => api.apiBilling(viewingKey!.id), enabled: connected && Boolean(viewingKey), retry: false, refetchInterval: viewingKey ? 10000 : false })
  const issueMutation = useMutation({
    mutationFn: ({ customerName, billingMode, billingAmountMinor, name, tags, numbers, voiceAgentId, postCallAgentId, credentials, voiceLocation, postCallLocation }: { customerName: string; billingMode: 'prepaid' | 'postpaid'; billingAmountMinor: number; name: string; tags: string[]; numbers: RegisteredNumber[]; voiceAgentId: string | null; postCallAgentId: string | null; credentials: string; voiceLocation: string; postCallLocation: string }) => api.createApiKey(customerName, billingMode, billingAmountMinor, name, tags, numbers, voiceAgentId, postCallAgentId, credentials, voiceLocation, postCallLocation),
    onSuccess: result => {
      void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-agent-configs'] })
      setOneTimeKey(result)
      setAssignmentOpen(false)
      setSelectedNumbers([])
      setApiName(''); setApiTags(''); setApiCustomerName(''); setApiBillingMode('prepaid'); setApiBillingAmount('1000'); setVoiceAgentId(''); setPostCallAgentId(''); setApiCredentials('')
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
    mutationFn: ({ key, name, tags, numbers, voiceAgentId, postCallAgentId, credentials, voiceLocation, postCallLocation }: { key: PlatformApiKey; name: string; tags: string[]; numbers: RegisteredNumber[]; voiceAgentId: string | null; postCallAgentId: string | null; credentials: string; voiceLocation: string; postCallLocation: string }) => api.updateApiConfig(key.id, name, tags, numbers, voiceAgentId, postCallAgentId, credentials, voiceLocation, postCallLocation),
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
    onSuccess: () => { if (viewingKey) void queryClient.invalidateQueries({ queryKey: ['platform-api-billing', viewingKey.id] }); setBillingAdjustmentOpen(false); setToast('API billing balance or limit updated.') },
    onError: error => setToast(errorText(error)),
  })
  const saveAgentConfigMutation = useMutation({
    mutationFn: ({ id, data }: { id?: string; data: { name: string; kind: 'calling' | 'post_call'; provider: 'vertex'; model: string; location: string; cost_per_minute_minor: number; currency: string; tags: string[] } }) => id ? api.updateAgentConfig(id, data) : api.createAgentConfig(data),
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
    onSuccess: result => {
      setSimulationSessionId(result.session_id)
      void queryClient.invalidateQueries({ queryKey: ['simulation-call-logs', simulationApiId] })
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
    if (!apiName.trim()) { setToast('Enter a name for this API.'); return }
    if (!voiceAgentId && !postCallAgentId) { setToast('Select at least one agent for this API.'); return }
    if (!apiCredentials.trim()) { setToast('Add a Google Cloud service account JSON for this API.'); return }
    const configuredRate = (agentsQuery.data || []).filter(agent => agent.id === voiceAgentId || agent.id === postCallAgentId).reduce((sum, agent) => sum + agent.cost_per_minute_minor, 0)
    if (configuredRate < 1) { setToast('Selected agents need a positive combined INR per-minute cost before billing can be enabled.'); return }
    const amount = Number(apiBillingAmount)
    if (!Number.isFinite(amount) || amount <= 0) { setToast('Enter a positive INR starting balance or credit limit.'); return }
    issueMutation.mutate({ customerName: apiCustomerName.trim(), billingMode: apiBillingMode, billingAmountMinor: Math.round(amount * 100), name: apiName.trim(), tags: parseTags(apiTags), numbers: selected, voiceAgentId: voiceAgentId || null, postCallAgentId: postCallAgentId || null, credentials: apiCredentials, voiceLocation: voiceLocation.trim(), postCallLocation: postCallLocation.trim() })
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
  const startSimulation = () => {
    const key = (keysQuery.data || []).find(item => item.id === simulationApiId)
    if (!key || key.revoked_at) { setToast('Select an active API first.'); return }
    if (!simulationDestination.trim()) { setToast('Enter the destination phone number.'); return }
    if (!simulationCallerId) { setToast('Choose an assigned caller ID.'); return }
    if (!key.voice_agent_enabled) { setToast('This API has no calling agent enabled.'); return }
    if (/\b(transcript|post[- ]call|after the call|summari[sz]e the call)\b/i.test(simulationLiveInstructions)) {
      setToast('These look like post-call instructions. Put transcript extraction or call summaries in the Post-call task field.'); return
    }
    let liveContext: Record<string, unknown>
    let outputSchema: Record<string, unknown>
    try {
      liveContext = JSON.parse(simulationLiveContext || '{}')
      if (!liveContext || Array.isArray(liveContext) || typeof liveContext !== 'object') throw new Error('Live-call context must be a JSON object.')
      outputSchema = JSON.parse(simulationOutputSchema)
      if (!outputSchema || Array.isArray(outputSchema) || typeof outputSchema !== 'object') throw new Error('Output schema must be a JSON object.')
    } catch (error) { setToast(error instanceof Error ? error.message : 'Check the JSON fields.'); return }
    const [provider] = simulationCallerId.split('|')
    const data: Record<string, unknown> = {
      provider, channel: 'phone', destination: simulationDestination.trim(), caller_id: simulationCallerId.split('|')[1],
      live_agent: { agent_id: simulationProfileId || null, instructions: simulationLiveInstructions, context: liveContext },
      recording_allowed: simulationRecording,
    }
    if (key.post_call_agent_enabled) data.post_call = {
      context: simulationPostCallContext.trim(), output_schema: outputSchema, tools: simulationTools,
      time_zone: simulationTimeZone, time_format: simulationTimeFormat,
      include_live_agent_context: simulationIncludeLiveContext,
    }
    setSimulationSessionId('')
    simulateCallMutation.mutate({ keyId: key.id, data })
  }
  const simulationCall = simulationCallsQuery.data?.items.find(item => item.session_id === simulationSessionId)
  const handleCreateNumberSelection = (value: string[] | string) => {
    const values = typeof value === 'string' ? value.split(',') : value
    setSelectedNumbers(values)
    const availableAgents = agentsQuery.data || []
    if (!availableAgents.some(agent => agent.id === voiceAgentId && agent.kind === 'calling')) setVoiceAgentId(availableAgents.find(agent => agent.kind === 'calling')?.id || '')
    if (!availableAgents.some(agent => agent.id === postCallAgentId && agent.kind === 'post_call')) setPostCallAgentId(availableAgents.find(agent => agent.kind === 'post_call')?.id || '')
    setVoiceLocation(availableAgents.find(agent => agent.id === voiceAgentId)?.location || availableAgents.find(agent => agent.kind === 'calling')?.location || 'us-central1')
    setPostCallLocation(availableAgents.find(agent => agent.id === postCallAgentId)?.location || availableAgents.find(agent => agent.kind === 'post_call')?.location || 'us-central1')
  }
  const openCreateApi = () => {
    setSelectedNumbers([]); setApiName(''); setApiTags(''); setApiCustomerName(''); setApiBillingMode('prepaid'); setApiBillingAmount('1000'); setVoiceAgentId(''); setPostCallAgentId(''); setApiCredentials(''); setVoiceLocation('us-central1'); setPostCallLocation('us-central1'); setAssignmentOpen(true)
  }
  const openEditApi = (key: PlatformApiKey) => {
    setManagementKey(key); setSelectedNumbers(key.assigned_numbers.map(number => `${number.provider}|${number.phone_number}`))
    setApiName(key.name); setApiTags((key.tags || []).join(', ')); setVoiceAgentId(key.voice_agent_id || ''); setPostCallAgentId(key.post_call_agent_id || '')
    setApiCredentials(''); setVoiceLocation(key.voice_location || key.vertex_location || 'us-central1'); setPostCallLocation(key.post_call_location || key.vertex_location || 'us-central1')
  }
  const openCreateAgentConfig = () => { setEditingAgentConfig(null); setAgentName(''); setAgentTags(''); setAgentKind('calling'); setAgentModel('google/gemini-live-2.5-flash-native-audio'); setAgentLocation('us-central1'); setAgentCost('0.00'); setAgentCurrency('INR'); setAgentConfigOpen(true) }
  const openEditAgentConfig = (agent: AgentConfiguration) => { setEditingAgentConfig(agent); setAgentName(agent.name); setAgentTags((agent.tags || []).join(', ')); setAgentKind(agent.kind); setAgentModel(agent.model); setAgentLocation(agent.location); setAgentCost((agent.cost_per_minute_minor / 100).toFixed(2)); setAgentCurrency(agent.currency); setAgentConfigOpen(true) }
  const saveAgentConfig = () => {
    if (!agentName.trim() || !agentModel.trim() || !agentLocation.trim()) { setToast('Complete all required agent fields.'); return }
    const cost = Number(agentCost)
    if (!Number.isFinite(cost) || cost < 0) { setToast('Enter a valid non-negative per-minute cost.'); return }
    saveAgentConfigMutation.mutate({ id: editingAgentConfig?.id, data: { name: agentName.trim(), kind: agentKind, provider: 'vertex', model: agentModel.trim(), location: agentLocation.trim(), cost_per_minute_minor: Math.round(cost * 100), currency: agentCurrency.toUpperCase(), tags: parseTags(agentTags) } })
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
        {page === 'API' && <ApiPage connected={connected} numbers={outboundNumbers} keys={keysQuery.data || []} agents={agentsQuery.data || []} loading={keysQuery.isLoading} error={keysQuery.error} onCreate={openCreateApi} onView={key => setViewingKey(key)} onManage={openEditApi} onDelete={id => { const apiKey = (keysQuery.data || []).find(key => key.id === id); if (apiKey) openDeleteConfirmation({ kind: 'api', api: apiKey }) }} />}
        {page === 'Agents' && <AgentsPage agents={agentsQuery.data || []} loading={agentsQuery.isLoading} error={agentsQuery.error} onCreate={openCreateAgentConfig} onEdit={openEditAgentConfig} onDelete={agent => openDeleteConfirmation({ kind: 'agent', agent })} />}
        {page === 'Simulation' && <SimulationPage keys={keysQuery.data || []} loading={keysQuery.isLoading} error={keysQuery.error} selectedApiId={simulationApiId} onSelectApi={selectSimulationApi} destination={simulationDestination} onDestinationChange={setSimulationDestination} callerId={simulationCallerId} onCallerIdChange={setSimulationCallerId} profiles={simulationProfilesQuery.data || []} profilesLoading={simulationProfilesQuery.isLoading} profileId={simulationProfileId} onProfileChange={selectSimulationProfile} liveInstructions={simulationLiveInstructions} onLiveInstructionsChange={setSimulationLiveInstructions} liveContext={simulationLiveContext} onLiveContextChange={setSimulationLiveContext} postCallContext={simulationPostCallContext} onPostCallContextChange={setSimulationPostCallContext} outputSchema={simulationOutputSchema} onOutputSchemaChange={setSimulationOutputSchema} tools={simulationTools} onToolsChange={setSimulationTools} timeZone={simulationTimeZone} onTimeZoneChange={setSimulationTimeZone} timeFormat={simulationTimeFormat} onTimeFormatChange={setSimulationTimeFormat} includeLiveContext={simulationIncludeLiveContext} onIncludeLiveContextChange={setSimulationIncludeLiveContext} recording={simulationRecording} onRecordingChange={setSimulationRecording} call={simulationCall} callLoading={simulationCallsQuery.isFetching} onStart={startSimulation} submitting={simulateCallMutation.isPending} />}
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
        <TextField label="Customer name" value={apiCustomerName} onChange={event => setApiCustomerName(event.target.value)} required fullWidth />
        <FormControl fullWidth><InputLabel id="api-billing-mode-label">Billing type</InputLabel><Select labelId="api-billing-mode-label" label="Billing type" value={apiBillingMode} onChange={event => setApiBillingMode(event.target.value as 'prepaid' | 'postpaid')}><MenuItem value="prepaid">Prepaid</MenuItem><MenuItem value="postpaid">Postpaid</MenuItem></Select></FormControl>
        <TextField label={apiBillingMode === 'prepaid' ? 'Starting prepaid balance (INR)' : 'Postpaid credit limit (INR)'} type="number" value={apiBillingAmount} onChange={event => setApiBillingAmount(event.target.value)} inputProps={{ min: 0.01, step: 0.01 }} required fullWidth helperText="This balance or limit is isolated to this API customer." />
        <TextField label="API name" value={apiName} onChange={event => setApiName(event.target.value)} required fullWidth />
        <TextField label="Tags" value={apiTags} onChange={event => setApiTags(event.target.value)} fullWidth placeholder="clinic, priority, us-east" helperText="Optional. Separate tags with commas." />
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
        <TextField label="Google Cloud service account JSON" value={apiCredentials} onChange={event => setApiCredentials(event.target.value)} multiline minRows={5} required fullWidth placeholder="Paste the service account JSON for this API's Vertex project" />
        {!voiceAgentId && postCallAgentId && <Alert severity="warning">This API has only a post-call agent assigned. Outbound phone calls require a calling agent.</Alert>}
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setAssignmentOpen(false)}>Cancel</Button><Button variant="contained" disabled={issueMutation.isPending || !selected.length || !apiCustomerName.trim() || !apiName.trim() || (!voiceAgentId && !postCallAgentId) || !apiCredentials.trim() || Number(apiBillingAmount) <= 0} onClick={submitCreateApi}>{issueMutation.isPending ? 'Creating…' : 'Create API'}</Button></DialogActions>
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
        <Alert severity="info" sx={{ fontSize: 12 }}>Agent model and default location are configured in Agents. This API stores its own Google credentials and may override each agent's location.</Alert>
        <TextField label="API name" value={apiName} onChange={event => setApiName(event.target.value)} required fullWidth />
        <TextField label="Tags" value={apiTags} onChange={event => setApiTags(event.target.value)} fullWidth placeholder="clinic, priority, us-east" helperText="Optional. Separate tags with commas." />
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
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setManagementKey(null)}>Cancel</Button><Button variant="contained" disabled={!managementKey || !selectedNumbers.length || !apiName.trim() || (!voiceAgentId && !postCallAgentId) || manageNumbersMutation.isPending} onClick={() => { if (!managementKey) return; const numbers = currentNumbers.filter(number => selectedNumbers.includes(`${number.provider}|${number.phone_number}`)); manageNumbersMutation.mutate({ key: managementKey, name: apiName.trim(), tags: parseTags(apiTags), numbers, voiceAgentId: voiceAgentId || null, postCallAgentId: postCallAgentId || null, credentials: apiCredentials, voiceLocation: voiceLocation.trim(), postCallLocation: postCallLocation.trim() }) }}>{manageNumbersMutation.isPending ? 'Saving…' : 'Save API configuration'}</Button></DialogActions>
    </Dialog>

    <Dialog open={agentConfigOpen} onClose={() => setAgentConfigOpen(false)} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>{editingAgentConfig ? 'Edit agent configuration' : 'Create agent configuration'}</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="info">Agent templates are owned by the platform and can be assigned to customer APIs. Provider, model, default location, and INR cost are managed here; Google Cloud credentials belong to each API.</Alert>
        <TextField label="Agent name" value={agentName} onChange={event => setAgentName(event.target.value)} required fullWidth />
        <TextField label="Tags" value={agentTags} onChange={event => setAgentTags(event.target.value)} fullWidth placeholder="support, multilingual, v2" helperText="Optional. Separate tags with commas." />
        <FormControl fullWidth><InputLabel id="agent-kind-label">Agent type</InputLabel><Select labelId="agent-kind-label" label="Agent type" value={agentKind} onChange={event => { const kind = event.target.value as 'calling' | 'post_call'; setAgentKind(kind); if (!editingAgentConfig) setAgentModel(kind === 'calling' ? 'google/gemini-live-2.5-flash-native-audio' : 'gemini-2.5-flash') }}><MenuItem value="calling">Calling agent</MenuItem><MenuItem value="post_call">Post-call agent</MenuItem></Select></FormControl>
        <FormControl fullWidth><InputLabel id="agent-provider-label">Provider</InputLabel><Select labelId="agent-provider-label" label="Provider" value="vertex" disabled><MenuItem value="vertex">Google Vertex AI</MenuItem></Select></FormControl>
        <TextField label="Model ID" value={agentModel} onChange={event => setAgentModel(event.target.value)} required fullWidth />
        <TextField label="Vertex AI location" value={agentLocation} onChange={event => setAgentLocation(event.target.value)} required fullWidth />
        <Stack direction={{ xs: 'column', sm: 'row' }} gap={1.5}>
          <TextField label="Cost per minute" type="number" value={agentCost} onChange={event => setAgentCost(event.target.value)} inputProps={{ min: 0, step: 0.01 }} helperText="Configured agent cost; informational and separate from the customer billing plan." fullWidth />
          <TextField label="Currency" value="INR" InputProps={{ readOnly: true }} helperText="Cost is recorded in rupees (paise precision)." sx={{ minWidth: 130 }} />
        </Stack>
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setAgentConfigOpen(false)}>Cancel</Button><Button variant="contained" disabled={saveAgentConfigMutation.isPending} onClick={saveAgentConfig}>{saveAgentConfigMutation.isPending ? 'Saving…' : 'Save agent'}</Button></DialogActions>
    </Dialog>

    <Dialog open={Boolean(viewingKey)} onClose={() => setViewingKey(null)} maxWidth="lg" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>API details · {viewingKey?.name}</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Stack direction={{ xs: 'column', sm: 'row' }} gap={1.5} flexWrap="wrap">
          <Card variant="outlined" sx={{ p: 1.5, flex: 1, minWidth: 180 }}><Typography variant="caption">API key</Typography><Typography><code>{viewingKey?.key_prefix}</code></Typography></Card>
          <Card variant="outlined" sx={{ p: 1.5, flex: 1, minWidth: 180 }}><Typography variant="caption">Assigned numbers</Typography><Typography>{viewingKey?.assigned_numbers.map(item => item.phone_number).join(', ') || 'None'}</Typography></Card>
          <Card variant="outlined" sx={{ p: 1.5, flex: 1, minWidth: 180 }}><Typography variant="caption">Calling agent</Typography><Typography>{agentsQuery.data?.find(agent => agent.id === viewingKey?.voice_agent_id)?.name || 'Not assigned'}</Typography></Card>
          <Card variant="outlined" sx={{ p: 1.5, flex: 1, minWidth: 180 }}><Typography variant="caption">Post-call agent</Typography><Typography>{agentsQuery.data?.find(agent => agent.id === viewingKey?.post_call_agent_id)?.name || 'Not assigned'}</Typography></Card>
        </Stack>
        <Typography variant="h6" sx={{ fontWeight: 800 }}>Calls for this API</Typography>
        {apiCallsQuery.isLoading ? <CircularProgress size={22} /> : apiCallsQuery.error ? <Alert severity="error">{errorText(apiCallsQuery.error)}</Alert> : apiCallsQuery.data?.items.length ? <TableContainer sx={{ width: "100%", overflowX: "auto" }}><Table size="small" sx={{ minWidth: 900 }}><TableHead><TableRow>{['Started', 'Direction', 'From → To', 'Provider', 'Status', 'Duration', 'Post-call result', 'Session'].map(item => <TableCell className="table-head" key={item}>{item}</TableCell>)}</TableRow></TableHead><TableBody>{apiCallsQuery.data.items.map(call => <TableRow key={call.session_id}><TableCell className="table-cell">{new Date(call.started_at).toLocaleString()}</TableCell><TableCell className="table-cell">{call.direction || '—'}</TableCell><TableCell className="table-cell">{call.caller || '—'} → {call.callee || '—'}</TableCell><TableCell className="table-cell">{call.provider || '—'}</TableCell><TableCell className="table-cell"><StatusChip enabled={call.status === 'completed'} label={call.status} /></TableCell><TableCell className="table-cell">{call.duration_seconds == null ? '—' : `${call.duration_seconds}s`}</TableCell><TableCell className="table-cell">{call.post_call_status ? <><StatusChip enabled={call.post_call_status === 'completed'} label={call.post_call_status} />{call.post_call_result && <details style={{ marginTop: 6 }}><summary>View result</summary><pre style={{ whiteSpace: 'pre-wrap', maxWidth: 320, fontSize: 11 }}>{JSON.stringify(call.post_call_result, null, 2)}</pre></details>}</> : call.post_call_agent_enabled === false ? <StatusChip enabled={false} label="disabled" /> : 'Waiting for call to finish'}</TableCell><TableCell className="table-cell"><code>{call.session_id}</code></TableCell></TableRow>)}</TableBody></Table></TableContainer> : <div className="empty-state">No calls yet. Use the API key and the request example on the Docs page to place a test call.</div>}
        <Typography variant="h6" sx={{ fontWeight: 800, mt: 1 }}>Billing for this API</Typography>
        {apiBillingQuery.isLoading ? <CircularProgress size={22} /> : apiBillingQuery.error ? <Alert severity="error">{errorText(apiBillingQuery.error)}</Alert> : apiBillingQuery.data?.configured ? <>
          <Alert severity="info">Calls, rate, balance or credit limit, and ledger are isolated to this API customer. The per-minute rate is snapshotted from the selected agent costs when the API is created.</Alert>
          <Stack direction={{ xs: 'column', sm: 'row' }} gap={1.5} flexWrap="wrap">
            <UsageCard label="Calls" value={String(apiBillingQuery.data.calls ?? 0)} />
            <UsageCard label="Billable time" value={`${Math.floor((apiBillingQuery.data.billable_seconds || 0) / 60)}m ${(apiBillingQuery.data.billable_seconds || 0) % 60}s`} />
            <UsageCard label="Attributed charges" value={formatMoney(apiBillingQuery.data.charges_minor || 0, apiBillingQuery.data.currency || 'INR')} />
            <UsageCard label="API billing plan" value={`${apiBillingQuery.data.billing_mode} · ${formatMoney(apiBillingQuery.data.rate_per_minute_minor || 0, apiBillingQuery.data.currency || 'INR')}/min`} />
            <UsageCard label="API customer balance" value={apiBillingQuery.data.billing_mode === 'prepaid' ? formatMoney(apiBillingQuery.data.customer_prepaid_balance_minor || 0, apiBillingQuery.data.currency || 'INR') : `${formatMoney(apiBillingQuery.data.customer_postpaid_used_minor || 0, apiBillingQuery.data.currency || 'INR')} used / ${formatMoney(apiBillingQuery.data.customer_postpaid_credit_limit_minor || 0, apiBillingQuery.data.currency || 'INR')} limit`} />
          </Stack>
          <Button variant="outlined" sx={{ alignSelf: 'flex-start' }} onClick={() => { const minor = apiBillingQuery.data?.billing_mode === 'prepaid' ? apiBillingQuery.data.customer_prepaid_balance_minor : apiBillingQuery.data?.customer_postpaid_credit_limit_minor; setBillingAdjustmentAmount(((minor || 0) / 100).toFixed(2)); setBillingAdjustmentOpen(true) }}>Adjust balance / credit limit</Button>
        </> : <Alert severity="warning">{apiBillingQuery.data?.message || 'No billing plan is configured for this API customer yet.'}</Alert>}
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setViewingKey(null)}>Close</Button><Button onClick={() => { if (viewingKey) openEditApi(viewingKey); setViewingKey(null) }}>Edit API</Button></DialogActions>
    </Dialog>

    <Dialog open={billingAdjustmentOpen} onClose={() => setBillingAdjustmentOpen(false)} maxWidth="xs" fullWidth>
      <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>{apiBillingQuery.data?.billing_mode === 'prepaid' ? 'Set prepaid balance' : 'Set postpaid credit limit'}</DialogTitle>
      <DialogContent><Stack gap={2} sx={{ mt: 1 }}>
        <Alert severity="info">This updates billing for {viewingKey?.customer_name || 'this API customer'} only. Amounts are in INR.</Alert>
        <TextField autoFocus fullWidth type="number" label={apiBillingQuery.data?.billing_mode === 'prepaid' ? 'Prepaid balance (INR)' : 'Credit limit (INR)'} value={billingAdjustmentAmount} onChange={event => setBillingAdjustmentAmount(event.target.value)} inputProps={{ min: 0, step: 0.01 }} />
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setBillingAdjustmentOpen(false)}>Cancel</Button><Button variant="contained" disabled={adjustBillingMutation.isPending || !billingAdjustmentAmount || Number(billingAdjustmentAmount) < 0 || !Number.isFinite(Number(billingAdjustmentAmount))} onClick={() => { if (!viewingKey || !apiBillingQuery.data?.billing_mode) return; adjustBillingMutation.mutate({ key: viewingKey, mode: apiBillingQuery.data.billing_mode, amountMinor: Math.round(Number(billingAdjustmentAmount) * 100) }) }}>{adjustBillingMutation.isPending ? 'Saving…' : 'Save billing'}</Button></DialogActions>
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

function AgentsPage({ agents, loading, error, onEdit, onCreate, onDelete }: { agents: AgentConfiguration[]; loading: boolean; error: unknown; onEdit: (agent: AgentConfiguration) => void; onCreate: () => void; onDelete: (agent: AgentConfiguration) => void }) {
  return <><PageIntro eyebrow="AGENT CONFIGURATION" title="Agents" subtitle="Create platform-owned calling and post-call templates with provider, model, default location, and cost." action={<Button variant="contained" startIcon={<AddRoundedIcon />} onClick={onCreate}>Create agent</Button>} />
    <Alert severity="info" sx={{ mt: 2 }}>Google Vertex AI is currently available for calling and post-call agents. API credentials and location overrides are configured on each API.</Alert>
    <Card className="surface-card" sx={{ mt: 2 }}>
      {loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : agents.length ? <TableContainer sx={{ width: "100%", overflowX: "auto" }}><Table size="small" sx={{ minWidth: 900 }}><TableHead><TableRow>{['Agent', 'Type', 'Provider / model', 'Default location', 'Cost / minute', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{agents.map(agent => <TableRow key={agent.id} hover><TableCell className="table-cell"><b>{agent.name}</b><TagChips tags={agent.tags || []} /></TableCell><TableCell className="table-cell">{agent.kind === 'calling' ? 'Calling' : 'Post-call'}</TableCell><TableCell className="table-cell">{agent.provider} · {agent.model}</TableCell><TableCell className="table-cell">{agent.location}</TableCell><TableCell className="table-cell">{formatMoney(agent.cost_per_minute_minor, agent.currency)}/min</TableCell><TableCell className="table-cell"><Button size="small" onClick={() => onEdit(agent)}>Edit</Button><Button size="small" color="error" onClick={() => onDelete(agent)}>Delete</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><AutoAwesomeRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No agents configured</b><div style={{ fontSize: 12, marginTop: 6 }}>Create calling or post-call agents first, then assign them to an API.</div><Button variant="contained" sx={{ mt: 2 }} onClick={onCreate}>Create agent</Button></div>}
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
  onStart: () => void; submitting: boolean
}) {
  const selected = props.keys.find(key => key.id === props.selectedApiId)
  const assignedNumbers = selected?.assigned_numbers || []
  return <>
    <PageIntro eyebrow="DEVELOPER TESTING" title="Simulation" subtitle="Place a real test call through a selected API and inspect its post-call result." />
    <Alert severity="warning" sx={{ mt: 2 }}>This places a real outbound call. The selected API’s billing plan, agent costs, and recording policy apply.</Alert>
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
        <FormControl><Stack direction="row" alignItems="center" gap={1}><Checkbox checked={props.recording} onChange={event => props.onRecordingChange(event.target.checked)} /><Typography variant="body2">Record this test call (subject to the API billing plan)</Typography></Stack></FormControl>
        <Button variant="contained" startIcon={<ScienceRoundedIcon />} onClick={props.onStart} disabled={!selected || !selected.voice_agent_enabled || !props.callerId || !props.destination.trim() || props.submitting}>{props.submitting ? 'Starting call…' : 'Start test call'}</Button>
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
  return <><PageIntro eyebrow="DEVELOPER GUIDE" title="API documentation" subtitle="Start a call, check its progress, and receive structured post-call results." />
    <Stack gap={2} sx={{ mt: 3 }}>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Authentication and phone numbers</Typography><Typography sx={{ mt: 1 }}>Use the full API key shown once when the API was created. Send it as a Bearer token. It can only call from phone numbers assigned to that API. If you assigned multiple numbers for a provider, include <code>caller_id</code>.</Typography><Typography sx={{ mt: 1 }}>Create the API key in the API page. Credentials and phone number assignments are editable from the API’s detail view.</Typography></Card>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Start an outbound call</Typography><Typography sx={{ mt: 1, mb: 1.5 }}>The call request returns an accepted session ID quickly; it does not hold the HTTP connection open while the conversation runs. Use the Simulation page to run the same flow from the console with an API, prompts, and extraction schema.</Typography><TextField value={callBody} fullWidth multiline minRows={21} InputProps={{ readOnly: true }} /></Card>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Check call status and results</Typography><Typography sx={{ mt: 1 }}>Poll <code>GET /v1/calls/{'{session_id}'}</code> for lifecycle status and <code>GET /v1/calls/{'{session_id}'}/post-call</code> for analysis status and result. Post-call processing starts after the call finishes. The API view refreshes its call log automatically while open.</Typography><TextField value={`curl "$CHIEFVOICE_URL/v1/calls/$SESSION_ID" \\\n  -H "Authorization: Bearer $CHIEFVOICE_API_KEY"`} fullWidth multiline minRows={2} sx={{ mt: 1.5 }} InputProps={{ readOnly: true }} /></Card>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Context, output schema, and tools</Typography><Typography sx={{ mt: 1 }}>Keep live conversation context in <code>live_agent.context</code>. Put extraction instructions and the JSON output schema in <code>post_call</code>. The schema is validated with strict types before the result is stored or delivered. Supported post-call tools are <code>calculator</code> and <code>time</code>; when using the time tool, set an IANA timezone and a format containing <code>%z</code>.</Typography><Typography sx={{ mt: 1 }}>Use <code>include_live_agent_context</code> only when post-call extraction needs the live context too. Treat supplied context as reference data and provide an “unknown” option for facts the agent cannot establish.</Typography></Card>
      <Card className="surface-card" sx={{ p: 2.5 }}><Typography variant="h6" sx={{ fontWeight: 800 }}>Webhooks and polling</Typography><Typography sx={{ mt: 1 }}>Polling is available for call and post-call status. If a post-call webhook is configured, the platform sends a signed callback when analysis is ready. The recipient should deduplicate deliveries by event ID because failed delivery attempts are retried.</Typography></Card>
    </Stack>
  </>
}
function StatusChip({ enabled, label }: { enabled: boolean; label?: string }) { return <Chip size="small" label={label || (enabled ? 'Enabled' : 'Disabled')} color={enabled ? 'success' : 'default'} variant={enabled ? 'filled' : 'outlined'} sx={{ fontSize: 10, height: 22 }} /> }
function PageIntro({ eyebrow, title, subtitle, action }: { eyebrow: string; title: string; subtitle: string; action?: React.ReactNode }) { return <div style={{ display: 'flex', alignItems: 'end', justifyContent: 'space-between', gap: 18, flexWrap: 'wrap' }}><div><div className="eyebrow">{eyebrow}</div><Typography className="page-title" variant="h4">{title}</Typography><div className="page-subtitle">{subtitle}</div></div>{action}</div> }
