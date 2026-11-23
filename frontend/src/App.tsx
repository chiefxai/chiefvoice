import { Fragment, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import axios from 'axios'
import {
  Alert, Avatar, Button, Card, Checkbox, Chip, CircularProgress, Dialog, DialogActions,
  DialogContent, DialogTitle, Drawer, FormControl, IconButton, InputAdornment,
  InputLabel, ListItemText, Menu, MenuItem, Select, Snackbar, Stack, Switch, FormControlLabel, Table, TableBody, TableCell,
  TableHead, TableRow, TableContainer, TablePagination, Tabs, Tab, TextField, Tooltip, Typography,
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
import ScienceRoundedIcon from '@mui/icons-material/ScienceRounded'
import ArrowBackRoundedIcon from '@mui/icons-material/ArrowBackRounded'
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import DescriptionRoundedIcon from '@mui/icons-material/DescriptionRounded'
import SearchRoundedIcon from '@mui/icons-material/SearchRounded'
import ChevronLeftRoundedIcon from '@mui/icons-material/ChevronLeftRounded'
import ChevronRightRoundedIcon from '@mui/icons-material/ChevronRightRounded'
import LogoutRoundedIcon from '@mui/icons-material/LogoutRounded'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import LoginPage from './LoginPage'
import { clearLoginCallbackFromUrl, currentUser, finishLogin, login, logout } from './auth'

type Page = 'Numbers' | 'Agents' | 'API' | 'Simulation' | 'Docs'
const pagePaths: Record<Page, string> = {
  Numbers: '/numbers', Agents: '/agents', API: '/api', Simulation: '/simulation', Docs: '/docs',
}
const defaultSimulationPostCallContext = '{}'
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
type AgentPromptTemplate = {
  id: string; api_key_id?: string | null; kind: 'calling' | 'post_call'; name: string; instructions: string
  tools: { type: 'builtin'; name: string }[]; variables: Record<string, unknown>
  settings: Record<string, unknown>; revision: number; enabled: boolean; is_default?: boolean
}
type ApiAgentTemplates = {
  items?: AgentPromptTemplate[]
  calling: AgentPromptTemplate[]; post_call: AgentPromptTemplate[]
  default_calling_id: string | null; default_post_call_id: string | null
}
type IssuedApiKey = {
  key: string
  api_key_record: PlatformApiKey
  post_call_webhook?: { target_url: string; failure_target_url?: string | null; failure_custom_payload?: Record<string, unknown> | null; signing_secret: string; configured_at: string } | null
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
  callingTemplateId?: string; postCallTemplateId?: string
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
  agentTemplates: () => http.get<{ items: AgentPromptTemplate[] }>('/v1/admin/customers/agent-templates').then(r => r.data.items),
  createAgentTemplate: (data: Omit<AgentPromptTemplate, 'id' | 'revision' | 'enabled'>) => http.post<AgentPromptTemplate>('/v1/admin/customers/agent-templates', data).then(r => r.data),
  updateAgentTemplate: (id: string, data: Omit<AgentPromptTemplate, 'id' | 'revision' | 'enabled'>) => http.put<AgentPromptTemplate>(`/v1/admin/customers/agent-templates/${id}`, data).then(r => r.data),
  deleteAgentTemplate: (id: string) => http.delete(`/v1/admin/customers/agent-templates/${id}`),
  apiAgentTemplates: (id: string) => http.get<ApiAgentTemplates>(`/v1/admin/customers/api-keys/${id}/agent-templates`).then(r => r.data),
  createApiAgentTemplate: (id: string, data: Omit<AgentPromptTemplate, 'id' | 'revision' | 'enabled'>) => http.post<AgentPromptTemplate>(`/v1/admin/customers/api-keys/${id}/agent-templates`, data).then(r => r.data),
  updateApiAgentTemplate: (id: string, templateId: string, data: Omit<AgentPromptTemplate, 'id' | 'revision' | 'enabled'>) => http.put<AgentPromptTemplate>(`/v1/admin/customers/api-keys/${id}/agent-templates/${templateId}`, data).then(r => r.data),
  deleteApiAgentTemplate: (id: string, templateId: string) => http.delete(`/v1/admin/customers/api-keys/${id}/agent-templates/${templateId}`),
  setDefaultApiAgentTemplate: (id: string, templateId: string) => http.post<ApiAgentTemplates>(`/v1/admin/customers/api-keys/${id}/agent-templates/${templateId}/default`).then(r => r.data),
  setApiAgentTemplates: (id: string, data: { calling_template_ids: string[]; post_call_template_ids: string[]; default_calling_template_id: string | null; default_post_call_template_id: string | null }) => http.put<ApiAgentTemplates>(`/v1/admin/customers/api-keys/${id}/agent-templates`, data).then(r => r.data),
  apiCalls: (id: string, limit = 50, offset = 0) => http.get<{ items: ApiCallLog[]; total: number }>(`/v1/admin/customers/api-keys/${id}/calls`, { params: { limit, offset } }).then(r => r.data),
  apiCallDetail: (id: string, sessionId: string) => http.get<ApiCallDetail>(`/v1/admin/customers/api-keys/${id}/calls/${sessionId}`).then(r => r.data),
  apiCallRecording: (id: string, sessionId: string) => http.get<Blob>(`/v1/admin/customers/api-keys/${id}/calls/${sessionId}/recording`, { responseType: 'blob' }).then(r => r.data),
  voiceProfiles: (id: string) => http.get<{ items: VoiceAgentProfile[] }>(`/v1/admin/customers/api-keys/${id}/voice-profiles`).then(r => r.data.items),
  simulateCall: (id: string, data: Record<string, unknown>) => http.post<{ session_id: string; status: string }>(`/v1/admin/customers/api-keys/${id}/simulate`, data, { headers: { 'Idempotency-Key': crypto.randomUUID() } }).then(r => r.data),
  apiBilling: (id: string) => http.get<ApiBillingUsage>(`/v1/admin/customers/api-keys/${id}/billing`).then(r => r.data),
  adjustApiBilling: (id: string, mode: 'prepaid' | 'postpaid', amountMinor: number) => http.put(`/v1/admin/customers/api-keys/${id}/billing`, mode === 'prepaid' ? { prepaid_balance_minor: amountMinor } : { postpaid_credit_limit_minor: amountMinor }),
  customers: () => http.get<{ items: Customer[] }>('/v1/admin/customers').then(r => r.data.items),
  addNumbers: (data: { credential_mode: 'platform' | 'custom'; phone_numbers: string[]; standard_channels: number; unlimited_channels: number; tags: string[]; auth_id?: string; auth_token?: string }) => http.post('/v1/admin/customers/phone-numbers', data),
  updateNumber: async (number: RegisteredNumber, inbound: boolean, outbound: boolean, standardChannels: number, unlimitedChannels: number, tags: string[]) => {
    const body = {
      phone_number: number.phone_number,
      inbound_enabled: inbound,
      outbound_enabled: outbound,
      standard_channels: standardChannels,
      unlimited_channels: unlimitedChannels,
      tags,
    }
    const params = { customer_id: number.customer_id }
    try {
      // Keep the established route first so this frontend works with API
      // containers that have not yet received the body-based route.
      return await http.put(
        `/v1/admin/customers/phone-numbers/${encodeURIComponent(number.provider)}/${encodeURIComponent(number.phone_number)}`,
        body,
        { params },
      )
    } catch (error) {
      // Fall back to the body-based route if the old path route cannot decode
      // the international number. If the record is actually missing, the API's
      // response detail is returned by the existing mutation error handler.
      if (!axios.isAxiosError(error) || error.response?.status !== 404) {
        throw error
      }
      return http.put(
        `/v1/admin/customers/phone-numbers/${encodeURIComponent(number.provider)}`,
        body,
        { params },
      )
    }
  },
  deleteNumber: (number: RegisteredNumber) => http.delete(`/v1/admin/customers/phone-numbers/${encodeURIComponent(number.provider)}/${encodeURIComponent(number.phone_number)}`, { params: { customer_id: number.customer_id } }),
  createApiKey: (customerName: string, billingMode: 'prepaid' | 'postpaid', billingAmountMinor: number, tags: string[], numbers: RegisteredNumber[], voiceAgentId: string | null, postCallAgentId: string | null, credentials: string, voiceLocation: string, postCallLocation: string, recordingAllowed: boolean, postCallWebhookUrl: string, failureWebhookUrl: string, failureWebhookPayload: Record<string, unknown> | null) => http.post<IssuedApiKey>('/v1/admin/customers/api-keys', { customer_name: customerName, billing_mode: billingMode, prepaid_balance_minor: billingMode === 'prepaid' ? billingAmountMinor : 0, postpaid_credit_limit_minor: billingMode === 'postpaid' ? billingAmountMinor : 0, tags, phone_numbers: numbers.map(number => ({ provider: number.provider, phone_number: number.phone_number })), voice_agent_id: voiceAgentId, post_call_agent_id: postCallAgentId, gcp_credentials_json: credentials, voice_location: voiceLocation, post_call_location: postCallLocation, recording_allowed: recordingAllowed, post_call_webhook_url: postCallWebhookUrl.trim() || null, failure_webhook_url: failureWebhookUrl.trim() || null, failure_webhook_payload: failureWebhookPayload }).then(r => r.data),
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
  const apiRouteMatch = location.pathname.match(/^\/api\/([^/]+)(?:\/(calls))?\/?$/)
  const apiDetailId = apiRouteMatch?.[1] || null
  const isApiHistory = apiRouteMatch?.[2] === 'calls'
  const page = apiDetailId ? 'API' : pageFromPath(location.pathname)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [profileAnchor, setProfileAnchor] = useState<HTMLElement | null>(null)
  const [apiCallsPage, setApiCallsPage] = useState(0)
  const [apiCallsPageSize, setApiCallsPageSize] = useState(10)
  const [apiConfigurationOpen, setApiConfigurationOpen] = useState(false)
  const [promptTemplatesOpen, setPromptTemplatesOpen] = useState(false)
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
  const [apiFailureWebhookUrl, setApiFailureWebhookUrl] = useState('')
  const [apiFailureWebhookPayload, setApiFailureWebhookPayload] = useState('{}')
  const [apiBillingMode, setApiBillingMode] = useState<'prepaid' | 'postpaid'>('prepaid')
  const [apiBillingAmount, setApiBillingAmount] = useState('1000')
  const [apiRecordingAllowed, setApiRecordingAllowed] = useState(false)
  const [voiceAgentId, setVoiceAgentId] = useState('')
  const [postCallAgentId, setPostCallAgentId] = useState('')
  const [apiCallingTemplateIds, setApiCallingTemplateIds] = useState<string[]>([])
  const [apiPostCallTemplateIds, setApiPostCallTemplateIds] = useState<string[]>([])
  const [apiDefaultCallingTemplateId, setApiDefaultCallingTemplateId] = useState('')
  const [apiDefaultPostCallTemplateId, setApiDefaultPostCallTemplateId] = useState('')
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
  const [simulationCallingTemplateId, setSimulationCallingTemplateId] = useState('')
  const [simulationPostCallTemplateId, setSimulationPostCallTemplateId] = useState('')
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
    setApiConfigurationOpen(false)
    setPromptTemplatesOpen(false)
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
  const apiAgentTemplatesQuery = useQuery({ queryKey: ['api-agent-templates', managementKey?.id], queryFn: () => api.apiAgentTemplates(managementKey!.id), enabled: connected && Boolean(managementKey), retry: false })
  const simulationAgentTemplatesQuery = useQuery({ queryKey: ['api-agent-templates', 'simulation', simulationApiId], queryFn: () => api.apiAgentTemplates(simulationApiId), enabled: connected && page === 'Simulation' && Boolean(simulationApiId), retry: false })
  const simulationProfilesQuery = useQuery({ queryKey: ['simulation-voice-profiles', simulationApiId], queryFn: () => api.voiceProfiles(simulationApiId), enabled: connected && page === 'Simulation' && Boolean(simulationApiId), retry: false })
  const simulationCallsQuery = useQuery({ queryKey: ['simulation-call-logs', simulationApiId], queryFn: () => api.apiCalls(simulationApiId), enabled: connected && page === 'Simulation' && Boolean(simulationApiId), retry: false, refetchInterval: simulationSessionId ? 3000 : false })
  const apiCallsQuery = useQuery({ queryKey: ['platform-api-calls', apiDetailId, apiCallsPage, apiCallsPageSize], queryFn: () => api.apiCalls(apiDetailId!, apiCallsPageSize, apiCallsPage * apiCallsPageSize), enabled: connected && Boolean(apiDetailId), retry: false, refetchInterval: apiDetailId ? 5000 : false })
  const apiBillingQuery = useQuery({ queryKey: ['platform-api-billing', apiDetailId], queryFn: () => api.apiBilling(apiDetailId!), enabled: connected && Boolean(apiDetailId), retry: false, refetchInterval: apiDetailId ? 10000 : false })
  const apiDetailKey = keysQuery.data?.find(key => key.id === apiDetailId) || null
  const headerTitle = apiDetailId ? apiDetailKey?.name || 'API details' : page
  useEffect(() => {
    const assignments = apiAgentTemplatesQuery.data
    if (!managementKey || !assignments) return
    setApiCallingTemplateIds(assignments.calling.map(item => item.id))
    setApiPostCallTemplateIds(assignments.post_call.map(item => item.id))
    setApiDefaultCallingTemplateId(assignments.default_calling_id || '')
    setApiDefaultPostCallTemplateId(assignments.default_post_call_id || '')
  }, [managementKey?.id, apiAgentTemplatesQuery.data])
  const issueMutation = useMutation({
    mutationFn: ({ customerName, billingMode, billingAmountMinor, tags, numbers, voiceAgentId, postCallAgentId, credentials, voiceLocation, postCallLocation, recordingAllowed, webhookUrl, failureWebhookUrl, failureWebhookPayload }: { customerName: string; billingMode: 'prepaid' | 'postpaid'; billingAmountMinor: number; tags: string[]; numbers: RegisteredNumber[]; voiceAgentId: string | null; postCallAgentId: string | null; credentials: string; voiceLocation: string; postCallLocation: string; recordingAllowed: boolean; webhookUrl: string; failureWebhookUrl: string; failureWebhookPayload: Record<string, unknown> | null }) => api.createApiKey(customerName, billingMode, billingAmountMinor, tags, numbers, voiceAgentId, postCallAgentId, credentials, voiceLocation, postCallLocation, recordingAllowed, webhookUrl, failureWebhookUrl, failureWebhookPayload),
    onSuccess: async result => {
      void queryClient.invalidateQueries({ queryKey: ['platform-api-keys'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-phone-numbers'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-agent-configs'] })
      setOneTimeKey(result)
      setAssignmentOpen(false)
      setSelectedNumbers([])
      setApiName(''); setApiTags(''); setApiCustomerName(''); setApiWebhookUrl(''); setApiFailureWebhookUrl(''); setApiFailureWebhookPayload('{}'); setApiBillingMode('prepaid'); setApiBillingAmount('1000'); setVoiceAgentId(''); setPostCallAgentId(''); setApiCredentials(''); setApiRecordingAllowed(false)
      setApiCallingTemplateIds([]); setApiPostCallTemplateIds([]); setApiDefaultCallingTemplateId(''); setApiDefaultPostCallTemplateId('')
      setToast('API key created successfully. You can now add and manage prompt templates inside the API page.')
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
    mutationFn: async ({ key, name, tags, numbers, voiceAgentId, postCallAgentId, credentials, voiceLocation, postCallLocation, recordingAllowed, templates }: { key: PlatformApiKey; name: string; tags: string[]; numbers: RegisteredNumber[]; voiceAgentId: string | null; postCallAgentId: string | null; credentials: string; voiceLocation: string; postCallLocation: string; recordingAllowed: boolean; templates: { calling_template_ids: string[]; post_call_template_ids: string[]; default_calling_template_id: string | null; default_post_call_template_id: string | null } }) => {
      await api.updateApiConfig(key.id, name, tags, numbers, voiceAgentId, postCallAgentId, credentials, voiceLocation, postCallLocation, recordingAllowed)
      return api.setApiAgentTemplates(key.id, templates)
    },
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
  const isDeleteConfirmed = (input: string, phrase: string) => {
    const norm = (s: string) => s.trim().toLowerCase().replace(/\s+/g, ' ').replace(/\s*\/\s*/g, '/')
    const a = norm(input)
    const b = norm(phrase)
    return Boolean(a && (a === b || a === 'delete'))
  }
  const openDeleteConfirmation = (target: DeleteConfirmation) => {
    setDeleteConfirmation(target)
    setDeleteConfirmationText('')
  }
  const confirmDeleteResource = () => {
    if (!deleteConfirmation || !isDeleteConfirmed(deleteConfirmationText, deleteConfirmationPhrase)) return
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
    if (!apiCredentials.trim()) { setToast('Add a Google Cloud service account JSON for this API.'); return }
    const configuredRate = (agentsQuery.data || []).filter(agent => agent.id === voiceAgentId || agent.id === postCallAgentId).reduce((sum, agent) => sum + agent.cost_per_minute_minor, 0)
    if (configuredRate < 1) { setToast('Selected agents need a positive combined INR per-minute cost before billing can be enabled.'); return }
    const amount = Number(apiBillingAmount)
    if (!Number.isFinite(amount) || amount <= 0) { setToast('Enter a positive INR starting balance or credit limit.'); return }
    let failurePayload: Record<string, unknown> | null = null
    try {
      const parsed: unknown = JSON.parse(apiFailureWebhookPayload || '{}')
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('Failure fallback payload must be a JSON object.')
      failurePayload = Object.keys(parsed).length ? parsed as Record<string, unknown> : null
    } catch (error) { setToast(error instanceof Error ? error.message : 'Check the failure fallback JSON.'); return }
    if ((apiFailureWebhookUrl.trim() || failurePayload) && !apiWebhookUrl.trim()) { setToast('Add the Call outcome webhook URL before configuring a failure fallback.'); return }
    issueMutation.mutate({ customerName: apiCustomerName.trim(), billingMode: apiBillingMode, billingAmountMinor: Math.round(amount * 100), tags: parseTags(apiTags), numbers: selected, voiceAgentId: voiceAgentId || null, postCallAgentId: postCallAgentId || null, credentials: apiCredentials, voiceLocation: voiceLocation.trim(), postCallLocation: postCallLocation.trim(), recordingAllowed: apiRecordingAllowed, webhookUrl: apiWebhookUrl, failureWebhookUrl: apiFailureWebhookUrl, failureWebhookPayload: failurePayload })
  }
  const selectSimulationApi = (id: string) => {
    setSimulationApiId(id)
    setSimulationSessionId('')
    const key = (keysQuery.data || []).find(item => item.id === id)
    setSimulationCallerId(key?.assigned_numbers[0] ? `${key.assigned_numbers[0].provider}|${key.assigned_numbers[0].phone_number}` : '')
    setSimulationProfileId('')
    setSimulationLiveInstructions('')
    setSimulationLiveContext('{}')
    setSimulationPostCallContext('{}')
    setSimulationCallingTemplateId('')
    setSimulationPostCallTemplateId('')
    setSimulationOutputSchema(defaultSimulationOutputSchema)
    setSimulationTools([])
    setSimulationTimeZone('Asia/Kolkata')
    setSimulationTimeFormat('%Y-%m-%d %H:%M:%S %z')
    setSimulationIncludeLiveContext(false)
  }
  const startSimulation = (template?: SimulationTemplate) => {
    const config: Omit<SimulationTemplate, 'id' | 'name'> = template || {
      apiId: simulationApiId, destination: simulationDestination, callerId: simulationCallerId,
      profileId: simulationProfileId, liveInstructions: simulationLiveInstructions,
      liveContext: simulationLiveContext, postCallContext: simulationPostCallContext,
      outputSchema: simulationOutputSchema, tools: simulationTools, timeZone: simulationTimeZone,
      timeFormat: simulationTimeFormat, includeLiveContext: simulationIncludeLiveContext,
      recording: simulationRecording,
      callingTemplateId: simulationCallingTemplateId, postCallTemplateId: simulationPostCallTemplateId,
    }
    const key = (keysQuery.data || []).find(item => item.id === config.apiId)
    if (!key || key.revoked_at) { setToast('Select an active API first.'); return }
    if (!config.destination.trim()) { setToast('Enter the destination phone number.'); return }
    if (!config.callerId) { setToast('Choose an assigned caller ID.'); return }
    if (!key.voice_agent_enabled) { setToast('This API has no calling agent enabled.'); return }
    let liveContext: Record<string, unknown>
    let postCallContext: Record<string, unknown>
    let outputSchema: Record<string, unknown>
    try {
      liveContext = JSON.parse(config.liveContext || '{}')
      if (!liveContext || Array.isArray(liveContext) || typeof liveContext !== 'object') throw new Error('Live-call context must be a JSON object.')
      postCallContext = JSON.parse(config.postCallContext || '{}')
      if (!postCallContext || Array.isArray(postCallContext) || typeof postCallContext !== 'object') throw new Error('Post-call context must be a JSON object.')
      outputSchema = JSON.parse(config.outputSchema)
      if (!outputSchema || Array.isArray(outputSchema) || typeof outputSchema !== 'object') throw new Error('Output schema must be a JSON object.')
    } catch (error) { setToast(error instanceof Error ? error.message : 'Check the JSON fields.'); return }
    const data: Record<string, unknown> = {
      channel: 'phone', destination: config.destination.trim(), caller_id: config.callerId.split('|')[1],
      live_agent: { agent_id: config.profileId || null, template_id: config.callingTemplateId || null, context: liveContext },
      recording_allowed: config.recording,
    }
    if (key.post_call_agent_enabled) data.post_call = {
      template_id: config.postCallTemplateId || null, context: postCallContext, output_schema: outputSchema,
      ...(config.tools.length ? { tools: config.tools.map(name => ({ type: 'builtin', name })) } : {}),
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
    setSimulationCallingTemplateId(template.callingTemplateId || '')
    setSimulationPostCallTemplateId(template.postCallTemplateId || '')
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
    setSelectedNumbers([]); setApiName(''); setApiTags(''); setApiCustomerName(''); setApiWebhookUrl(''); setApiFailureWebhookUrl(''); setApiFailureWebhookPayload('{}'); setApiBillingMode('prepaid'); setApiBillingAmount('1000'); setVoiceAgentId(''); setPostCallAgentId(''); setApiCallingTemplateIds([]); setApiPostCallTemplateIds([]); setApiDefaultCallingTemplateId(''); setApiDefaultPostCallTemplateId(''); setApiCredentials(''); setVoiceLocation('us-central1'); setPostCallLocation('global'); setApiRecordingAllowed(false); setAssignmentOpen(true)
  }
  const openEditApi = (key: PlatformApiKey) => {
    setManagementKey(key); setSelectedNumbers(key.assigned_numbers.map(number => `${number.provider}|${number.phone_number}`))
    setApiName(key.name); setApiTags((key.tags || []).join(', ')); setVoiceAgentId(key.voice_agent_id || ''); setPostCallAgentId(key.post_call_agent_id || '')
    setApiCallingTemplateIds([]); setApiPostCallTemplateIds([]); setApiDefaultCallingTemplateId(''); setApiDefaultPostCallTemplateId('')
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
    setProfileAnchor(null)
    await logout().catch(() => undefined)
    sessionStorage.clear()
    setToken('')
    setAuthError('')
    void queryClient.clear()
    setPromptTemplatesOpen(false)
    setApiConfigurationOpen(false)
    navigate(pagePaths.Numbers, { replace: true })
  }
  const profileTrigger = <div className="sidebar-profile"><Tooltip title="Profile menu" placement="right"><IconButton onClick={event => setProfileAnchor(event.currentTarget)} aria-label="Open profile menu"><Avatar sx={{ width: 34, height: 34, bgcolor: '#e7eaff', color: '#465bd2', fontSize: 12, fontWeight: 800 }}>CV</Avatar></IconButton></Tooltip><Typography variant="caption" sx={{ color: '#dce5f3', fontWeight: 700 }}>Platform admin</Typography><Tooltip title="Sign out" placement="right"><IconButton aria-label="Sign out" onClick={() => { void disconnect() }} sx={{ color: '#aab5c7', '&:hover': { color: '#fff', backgroundColor: 'rgba(255,255,255,.08)' } }}><LogoutRoundedIcon fontSize="small" /></IconButton></Tooltip></div>
  const profileMenu = <Menu anchorEl={profileAnchor} open={Boolean(profileAnchor)} onClose={() => setProfileAnchor(null)} anchorOrigin={{ vertical: 'top', horizontal: 'right' }} transformOrigin={{ vertical: 'bottom', horizontal: 'left' }}>
    <MenuItem onClick={() => { onToggleTheme(); setProfileAnchor(null) }}>{mode === 'light' ? <DarkModeRoundedIcon fontSize="small" sx={{ mr: 1.5 }} /> : <LightModeRoundedIcon fontSize="small" sx={{ mr: 1.5 }} />}Switch to {mode === 'light' ? 'dark' : 'light'} theme</MenuItem>
  </Menu>
  const navigation = <><div className="brand"><div className="brand-mark"><HubRoundedIcon fontSize="small" /></div>{!sidebarCollapsed && <div className="brand-word">chief<span>voice</span></div>}<Tooltip title={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'} placement="right"><IconButton className="platform-collapse-button" size="small" aria-label={sidebarCollapsed ? 'Expand platform sidebar' : 'Collapse platform sidebar'} onClick={() => setSidebarCollapsed(value => !value)}>{sidebarCollapsed ? <ChevronRightRoundedIcon fontSize="small" /> : <ChevronLeftRoundedIcon fontSize="small" />}</IconButton></Tooltip></div><div className="nav-caption">{!sidebarCollapsed && 'Platform'}</div><div className="nav-list">{(['Numbers', 'Agents', 'API', 'Simulation', 'Docs'] as const).map(item => { const icon = item === 'Numbers' ? <HubRoundedIcon /> : item === 'Agents' ? <AutoAwesomeRoundedIcon /> : item === 'API' ? <KeyRoundedIcon /> : item === 'Simulation' ? <ScienceRoundedIcon /> : <ContentCopyRoundedIcon />; return <Tooltip key={item} title={sidebarCollapsed ? item : ''} placement="right"><Button aria-label={item} className={`nav-item ${page === item ? 'active' : ''}`} onClick={() => { navigate(pagePaths[item]); setDrawerOpen(false) }} startIcon={icon}>{!sidebarCollapsed && item}</Button></Tooltip>})}</div>{profileTrigger}</>

  if (!authReady || !connected) {
    return <LoginPage loading={!authReady} signingIn={signingIn} error={authError} onSignIn={() => { void connect() }} />
  }

  return <div className={`app-shell ${mode === 'dark' ? 'theme-dark' : ''}`}>
    <aside className={`sidebar ${sidebarCollapsed ? 'collapsed' : ''}`}>{navigation}</aside>
    <Drawer open={drawerOpen} onClose={() => setDrawerOpen(false)} PaperProps={{ sx: { width: sidebarCollapsed ? 82 : 248, bgcolor: '#101b2d', color: 'white', p: 2 } }}><div className={`sidebar open ${sidebarCollapsed ? 'collapsed' : ''}`} style={{ position: 'static', width: 'auto', minHeight: '100%', padding: 0 }}>{navigation}</div></Drawer>
    {profileMenu}
    <main className="main-area">
      <header className="topbar">
        <Stack direction="row" alignItems="center" gap={1}><IconButton className="mobile-menu" onClick={() => setDrawerOpen(true)}><MenuRoundedIcon /></IconButton><Typography variant="h6" className="topbar-title">{headerTitle}</Typography></Stack>
        <div className="topbar-actions">
          {page === 'Numbers' && <Button variant="contained" startIcon={<AddRoundedIcon />} onClick={event => setAddNumberAnchor(event.currentTarget)}>Add number</Button>}
          {page === 'Agents' && <Button variant="contained" startIcon={<AddRoundedIcon />} onClick={openCreateAgentConfig}>Add agent</Button>}
          {page === 'Simulation' && <Button variant="contained" startIcon={<ScienceRoundedIcon />} onClick={() => window.dispatchEvent(new Event('chiefvoice-open-call-setup'))}>Call setup</Button>}
          {page === 'API' && !apiDetailId && <Button variant="contained" startIcon={<AddRoundedIcon />} onClick={openCreateApi}>Create API</Button>}
          {page === 'API' && apiDetailId && <><Button variant="outlined" startIcon={<DescriptionRoundedIcon />} onClick={() => setPromptTemplatesOpen(true)}>Prompt templates</Button>{!isApiHistory && <Tooltip title="API configuration"><IconButton aria-label="API configuration" onClick={() => setApiConfigurationOpen(true)}><InfoOutlinedIcon /></IconButton></Tooltip>}<Tooltip title={isApiHistory ? 'Back to API overview' : 'Back to APIs'}><IconButton aria-label={isApiHistory ? 'Back to API overview' : 'Back to APIs'} onClick={() => navigate(isApiHistory ? `/api/${encodeURIComponent(apiDetailId)}` : pagePaths.API)}><ArrowBackRoundedIcon /></IconButton></Tooltip>{!isApiHistory && apiDetailKey && !apiDetailKey.revoked_at && <Button variant="contained" onClick={() => openEditApi(apiDetailKey)}>Edit API</Button>}</>}
          {page === 'Docs' && <Button component="a" href={`${API_BASE}/docs`} target="_blank" rel="noreferrer" variant="contained">API reference</Button>}
        </div>
      </header>
      <div className={`content ${page === 'Docs' ? 'content-docs' : ''}`}>
        {page === 'Numbers' && <NumbersPage connected={connected} onConnect={() => { void connect() }} numbers={currentNumbers} loading={numbersQuery.isLoading} error={numbersQuery.error} onManageNumber={number => { setManagedNumber(number); setManagedInbound(number.inbound_enabled); setManagedOutbound(number.outbound_enabled); setManagedStandardChannels(String(number.standard_channels)); setManagedUnlimitedChannels(String(number.unlimited_channels)); setManagedNumberTags((number.tags || []).join(', ')) }} />}
        {page === 'API' && (apiDetailId
          ? isApiHistory
            ? <ApiCallHistoryPage api={apiDetailKey} loading={keysQuery.isLoading} error={keysQuery.error} calls={apiCallsQuery.data?.items || []} callsTotal={apiCallsQuery.data?.total || 0} callsPage={apiCallsPage} callsPageSize={apiCallsPageSize} onCallsPageChange={setApiCallsPage} onCallsPageSizeChange={size => { setApiCallsPageSize(size); setApiCallsPage(0) }} callsLoading={apiCallsQuery.isLoading} callsError={apiCallsQuery.error} />
            : <ApiDetailsPage api={apiDetailKey} agents={agentsQuery.data || []} loading={keysQuery.isLoading} error={keysQuery.error} calls={apiCallsQuery.data?.items || []} callsTotal={apiCallsQuery.data?.total || 0} callsPage={apiCallsPage} callsPageSize={apiCallsPageSize} onCallsPageChange={setApiCallsPage} onCallsPageSizeChange={size => { setApiCallsPageSize(size); setApiCallsPage(0) }} callsLoading={apiCallsQuery.isLoading} callsError={apiCallsQuery.error} billing={apiBillingQuery.data} billingLoading={apiBillingQuery.isLoading} billingError={apiBillingQuery.error} onAdjustBilling={amount => { setBillingAdjustmentAmount(amount); setBillingAdjustmentOpen(true) }} configurationOpen={apiConfigurationOpen} onCloseConfiguration={() => setApiConfigurationOpen(false)} />
          : <ApiPage numbers={outboundNumbers} keys={keysQuery.data || []} agents={agentsQuery.data || []} loading={keysQuery.isLoading} error={keysQuery.error} onView={key => navigate(`/api/${encodeURIComponent(key.id)}`)} onManage={openEditApi} onDelete={id => { const apiKey = (keysQuery.data || []).find(key => key.id === id); if (apiKey) openDeleteConfirmation({ kind: 'api', api: apiKey }) }} />)}
        {page === 'Agents' && <AgentsPage agents={agentsQuery.data || []} loading={agentsQuery.isLoading} error={agentsQuery.error} onEdit={openEditAgentConfig} onDelete={agent => openDeleteConfirmation({ kind: 'agent', agent })} />}
        {page === 'Simulation' && <SimulationPage keys={keysQuery.data || []} loading={keysQuery.isLoading} error={keysQuery.error} selectedApiId={simulationApiId} onSelectApi={selectSimulationApi} destination={simulationDestination} onDestinationChange={setSimulationDestination} callerId={simulationCallerId} onCallerIdChange={setSimulationCallerId} profiles={simulationProfilesQuery.data || []} profilesLoading={simulationProfilesQuery.isLoading} profileId={simulationProfileId} onProfileChange={setSimulationProfileId} liveInstructions={simulationLiveInstructions} onLiveInstructionsChange={setSimulationLiveInstructions} liveContext={simulationLiveContext} onLiveContextChange={setSimulationLiveContext} postCallContext={simulationPostCallContext} onPostCallContextChange={setSimulationPostCallContext} outputSchema={simulationOutputSchema} onOutputSchemaChange={setSimulationOutputSchema} tools={simulationTools} onToolsChange={setSimulationTools} timeZone={simulationTimeZone} onTimeZoneChange={setSimulationTimeZone} timeFormat={simulationTimeFormat} onTimeFormatChange={setSimulationTimeFormat} includeLiveContext={simulationIncludeLiveContext} onIncludeLiveContextChange={setSimulationIncludeLiveContext} recording={simulationRecording} onRecordingChange={setSimulationRecording} call={simulationCall} callLoading={simulationCallsQuery.isFetching} onStart={startSimulation} onApplyTemplate={applySimulationTemplate} submitting={simulateCallMutation.isPending} templates={simulationAgentTemplatesQuery.data} templatesLoading={simulationAgentTemplatesQuery.isLoading} callingTemplateId={simulationCallingTemplateId} onCallingTemplateChange={setSimulationCallingTemplateId} postCallTemplateId={simulationPostCallTemplateId} onPostCallTemplateChange={setSimulationPostCallTemplateId} />}
        {page === 'Docs' && <DocsPage />}
      </div>
      {apiDetailId && <ApiTemplatesSection apiId={apiDetailId} templatesOpen={promptTemplatesOpen} onCloseTemplates={() => setPromptTemplatesOpen(false)} />}
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
        <Alert severity="info" sx={{ fontSize: 12 }}>Reusable prompt templates are scoped to this API and can be created and managed under the Templates section on the API page after creation.</Alert>
        {(() => { const rate = (agentsQuery.data || []).filter(agent => agent.id === voiceAgentId || agent.id === postCallAgentId).reduce((sum, agent) => sum + agent.cost_per_minute_minor, 0); return <Alert severity={rate > 0 ? 'info' : 'warning'}>API billing rate: <b>{formatMoney(rate, 'INR')}/minute</b>. It is calculated from the selected calling and post-call agent template costs.</Alert> })()}
        <TextField label="Call outcome webhook URL (optional)" type="url" value={apiWebhookUrl} onChange={event => setApiWebhookUrl(event.target.value)} fullWidth placeholder="https://your-service.example.com/webhooks/chiefvoice" helperText="Chiefvoice sends completed results and call or post-call failure events here. Failure notifications are sent directly without AI. A signing secret is shown once after creation." />
        <TextField label="Failure fallback webhook URL (optional)" type="url" value={apiFailureWebhookUrl} onChange={event => setApiFailureWebhookUrl(event.target.value)} fullWidth placeholder="Leave blank to use the Call outcome webhook URL" helperText="Used for failed calls and missing transcripts. The signed standard failure event is still included." />
        <TextField label="Custom failure payload (optional JSON)" value={apiFailureWebhookPayload} onChange={event => setApiFailureWebhookPayload(event.target.value)} multiline minRows={3} fullWidth helperText={'Added under "custom_payload" without replacing the call ID or failure code. Static JSON only; AI is not run to fill it.'} />
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
        {oneTimeKey?.post_call_webhook && <><Alert severity="warning">This signing secret is shown once and verifies deliveries to both webhook endpoints. Store it securely.</Alert><Typography variant="body2">Call outcome endpoint: <b>{oneTimeKey.post_call_webhook.target_url}</b></Typography>{oneTimeKey.post_call_webhook.failure_target_url && <Typography variant="body2">Failure fallback endpoint: <b>{oneTimeKey.post_call_webhook.failure_target_url}</b></Typography>}{oneTimeKey.post_call_webhook.failure_custom_payload && <TextField label="Custom failure payload" value={JSON.stringify(oneTimeKey.post_call_webhook.failure_custom_payload, null, 2)} fullWidth multiline minRows={3} InputProps={{ readOnly: true }} />}<TextField label="Webhook signing secret" value={oneTimeKey.post_call_webhook.signing_secret} fullWidth multiline minRows={2} InputProps={{ readOnly: true, endAdornment: <InputAdornment position="end"><Tooltip title="Copy signing secret"><IconButton onClick={() => { if (oneTimeKey?.post_call_webhook) void navigator.clipboard.writeText(oneTimeKey.post_call_webhook.signing_secret) }}><ContentCopyRoundedIcon /></IconButton></Tooltip></InputAdornment> }} /></>}
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
        <Alert severity="info" sx={{ fontSize: 12 }}>Reusable prompt templates are scoped to this API and can be created and managed under the Templates section on the API details page.</Alert>
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
      <DialogActions sx={{ p: 2.5 }}><Button onClick={() => setManagementKey(null)}>Cancel</Button><Button variant="contained" disabled={!managementKey || !selectedNumbers.length || !apiName.trim() || (!voiceAgentId && !postCallAgentId) || manageNumbersMutation.isPending} onClick={() => { if (!managementKey) return; const numbers = currentNumbers.filter(number => selectedNumbers.includes(`${number.provider}|${number.phone_number}`)); manageNumbersMutation.mutate({ key: managementKey, name: apiName.trim(), tags: parseTags(apiTags), numbers, voiceAgentId: voiceAgentId || null, postCallAgentId: postCallAgentId || null, credentials: apiCredentials, voiceLocation: voiceLocation.trim(), postCallLocation: postCallLocation.trim(), recordingAllowed: apiRecordingAllowed, templates: { calling_template_ids: apiCallingTemplateIds, post_call_template_ids: apiPostCallTemplateIds, default_calling_template_id: apiDefaultCallingTemplateId || null, default_post_call_template_id: apiDefaultPostCallTemplateId || null } }) }}>{manageNumbersMutation.isPending ? 'Saving…' : 'Save API configuration'}</Button></DialogActions>
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
        <Stack direction="row" alignItems="center" justifyContent="space-between" flexWrap="wrap" gap={1}>
          <Typography variant="body2">Type <code>{deleteConfirmationPhrase}</code> or <code>DELETE</code> to confirm.</Typography>
          <Button size="small" variant="text" sx={{ textTransform: 'none', py: 0, minWidth: 0 }} onClick={() => setDeleteConfirmationText(deleteConfirmationPhrase)}>Autofill</Button>
        </Stack>
        <TextField autoFocus fullWidth label="Confirmation" value={deleteConfirmationText} onChange={event => setDeleteConfirmationText(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && isDeleteConfirmed(deleteConfirmationText, deleteConfirmationPhrase)) confirmDeleteResource() }} />
      </Stack></DialogContent>
      <DialogActions sx={{ p: 2.5 }}>
        <Button disabled={deleteConfirmationPending} onClick={() => { setDeleteConfirmation(null); setDeleteConfirmationText('') }}>Cancel</Button>
        <Button color="error" variant="contained" disabled={!isDeleteConfirmed(deleteConfirmationText, deleteConfirmationPhrase) || deleteConfirmationPending} onClick={confirmDeleteResource}>{deleteConfirmationPending ? 'Deleting…' : 'Delete'}</Button>
      </DialogActions>
    </Dialog>

    <Snackbar open={Boolean(toast)} autoHideDuration={5000} onClose={() => setToast('')} anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}><Alert onClose={() => setToast('')} severity={toast.toLowerCase().includes('error') || toast.toLowerCase().includes('failed') || toast.toLowerCase().includes('required') ? 'error' : 'success'} variant="filled">{toast}</Alert></Snackbar>
  </div>
}

function NumbersPage({ connected, onConnect, numbers, loading, error, onManageNumber }: { connected: boolean; onConnect: () => void; numbers: RegisteredNumber[]; loading: boolean; error: unknown; onManageNumber: (number: RegisteredNumber) => void }) {
  const [page, setPage] = useState(0)
  const [rowsPerPage, setRowsPerPage] = useState(10)
  const visibleNumbers = numbers.slice(page * rowsPerPage, (page + 1) * rowsPerPage)
  return <><Card className="table-surface" sx={{ mt: 1 }}>
      {!connected ? <ConnectPrompt onConnect={onConnect} /> : loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : numbers.length ? <><TableContainer className="table-surface" sx={{ width: "100%", overflowX: "auto" }}><Table size="small" sx={{ minWidth: 1220 }}><TableHead><TableRow>{['Phone number', 'Provider', 'Tags', 'Owner', 'Credentials', 'Standard channels', 'Unlimited channels', 'Inbound', 'Outbound', 'API access', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{visibleNumbers.map(number => <TableRow key={`${number.provider}|${number.phone_number}|${number.customer_id}`} hover><TableCell className="table-cell" sx={{ fontWeight: 700 }}>{number.phone_number}</TableCell><TableCell className="table-cell"><Chip size="small" label={number.provider} variant="outlined" sx={{ textTransform: 'capitalize' }} /></TableCell><TableCell className="table-cell"><TagChips tags={number.tags || []} /></TableCell><TableCell className="table-cell">Platform</TableCell><TableCell className="table-cell">{number.credential_mode === 'platform' ? 'Platform' : number.credential_mode === 'custom' ? 'Custom account' : '—'}</TableCell><TableCell className="table-cell">{number.standard_channels}</TableCell><TableCell className="table-cell">{number.unlimited_channels}</TableCell><TableCell className="table-cell"><StatusChip enabled={number.inbound_enabled} /></TableCell><TableCell className="table-cell"><StatusChip enabled={number.outbound_enabled} /></TableCell><TableCell className="table-cell">{number.api_key_name || 'Unassigned'}</TableCell><TableCell className="table-cell"><Button size="small" onClick={() => onManageNumber(number)}>Manage</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer><TablePagination component="div" count={numbers.length} page={page} onPageChange={(_, value) => setPage(value)} rowsPerPage={rowsPerPage} onRowsPerPageChange={event => { setRowsPerPage(Number(event.target.value)); setPage(0) }} rowsPerPageOptions={[10, 25, 50]} /></> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><HubRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No phone numbers registered</b><div style={{ fontSize: 12, marginTop: 6 }}>Add a number to get started. You can use the platform Vobiz account or add a custom provider account owned by the platform.</div></div>}
    </Card>
  </>
}

function ApiPage({ numbers, keys, agents, loading, error, onView, onManage, onDelete }: { numbers: RegisteredNumber[]; keys: PlatformApiKey[]; agents: AgentConfiguration[]; loading: boolean; error: unknown; onView: (key: PlatformApiKey) => void; onManage: (key: PlatformApiKey) => void; onDelete: (id: string) => void }) {
  const [page, setPage] = useState(0)
  const [rowsPerPage, setRowsPerPage] = useState(10)
  const visibleKeys = keys.slice(page * rowsPerPage, (page + 1) * rowsPerPage)
  return <><Card className="table-surface" sx={{ mt: 1 }}>
      {loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : keys.length ? <><TableContainer className="table-surface" sx={{ width: "100%", overflowX: "auto" }}><Table size="small" sx={{ minWidth: 900 }}><TableHead><TableRow>{['API name', 'Phone numbers', 'Customer', 'Calling agent', 'Post-call agent', 'Created', 'State', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{visibleKeys.map(key => <TableRow key={key.id} hover><TableCell className="table-cell"><b>{key.name}</b><br /><Typography variant="caption"><code>{key.key_prefix}</code></Typography><TagChips tags={key.tags || []} /></TableCell><TableCell className="table-cell" sx={{ fontWeight: 700 }}>{key.assigned_numbers.length ? key.assigned_numbers.map(number => number.phone_number).join(', ') : 'Unassigned'}</TableCell><TableCell className="table-cell">{key.customer_name}</TableCell><TableCell className="table-cell">{agents.find(agent => agent.id === key.voice_agent_id)?.name || 'None'}</TableCell><TableCell className="table-cell">{agents.find(agent => agent.id === key.post_call_agent_id)?.name || 'None'}</TableCell><TableCell className="table-cell">{new Date(key.created_at).toLocaleDateString()}</TableCell><TableCell className="table-cell"><StatusChip enabled={!key.revoked_at} label={key.revoked_at ? 'Revoked' : 'Active'} /></TableCell><TableCell className="table-cell"><Button size="small" onClick={() => onView(key)}>View</Button>{!key.revoked_at && <><Button size="small" onClick={() => onManage(key)}>Edit</Button><Button color="error" size="small" onClick={() => onDelete(key.id)}>Delete</Button></>}</TableCell></TableRow>)}</TableBody></Table></TableContainer><TablePagination component="div" count={keys.length} page={page} onPageChange={(_, value) => setPage(value)} rowsPerPage={rowsPerPage} onRowsPerPageChange={event => { setRowsPerPage(Number(event.target.value)); setPage(0) }} rowsPerPageOptions={[10, 25, 50]} /></> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><KeyRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No APIs created</b><div style={{ fontSize: 12, marginTop: 6 }}>{numbers.length ? 'Create an API by naming it, choosing phone numbers, and assigning configured agents.' : 'Register an outbound-enabled phone number first.'}</div></div>}
    </Card>
  </>
}

function ApiDetailsPage({ api: apiKey, agents, loading, error, calls, callsTotal, callsPage, callsPageSize, onCallsPageChange, onCallsPageSizeChange, callsLoading, callsError, billing, billingLoading, billingError, onAdjustBilling, configurationOpen, onCloseConfiguration }: {
  api: PlatformApiKey | null; agents: AgentConfiguration[]; loading: boolean; error: unknown
  calls: ApiCallLog[]; callsTotal: number; callsPage: number; callsPageSize: number
  onCallsPageChange: (page: number) => void; onCallsPageSizeChange: (size: number) => void; callsLoading: boolean; callsError: unknown
  billing?: ApiBillingUsage; billingLoading: boolean; billingError: unknown
  onAdjustBilling: (amount: string) => void; configurationOpen: boolean; onCloseConfiguration: () => void
}) {
  const voiceAgent = agents.find(agent => agent.id === apiKey?.voice_agent_id)
  const postCallAgent = agents.find(agent => agent.id === apiKey?.post_call_agent_id)
  return <>
    {loading ? <Card className="surface-card" sx={{ p: 3 }}><CircularProgress size={24} /></Card> : error ? <Alert severity="error">{errorText(error)}</Alert> : !apiKey ? <Alert severity="warning">This API could not be found. It may have been deleted.</Alert> : <Stack gap={2}>
      <Card className="surface-card" sx={{ p: { xs: 2, md: 2.5 } }}>
        <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" alignItems={{ sm: 'center' }} gap={1} sx={{ mb: 1.5 }}><Stack gap={0.25}><Typography variant="h6" sx={{ fontWeight: 800 }}>Billing overview</Typography><Typography variant="body2" color="text.secondary">{apiKey.customer_name} · <StatusChip enabled={!apiKey.revoked_at} label={apiKey.revoked_at ? 'Revoked' : 'Active'} /></Typography></Stack>{billing?.configured && billing.billing_mode && <Chip color="primary" variant="outlined" label={`${billing.billing_mode} plan`} sx={{ textTransform: 'capitalize' }} />}</Stack>
        {billingLoading ? <CircularProgress size={22} /> : billingError ? <Alert severity="error">{errorText(billingError)}</Alert> : billing?.configured ? <>
          <div className="api-kpi-grid"><UsageCard label="Charges" value={formatMoney(billing.charges_minor || 0, billing.currency || 'INR')} /><UsageCard label="Billed time" value={`${Math.floor((billing.billable_seconds || 0) / 60)}m ${(billing.billable_seconds || 0) % 60}s`} /><UsageCard label="Total calls" value={String(billing.calls ?? 0)} /><UsageCard label={billing.billing_mode === 'prepaid' ? 'Available balance' : 'Available credit'} value={billing.billing_mode === 'prepaid' ? formatMoney(billing.customer_prepaid_balance_minor || 0, billing.currency || 'INR') : formatMoney(Math.max(0, (billing.customer_postpaid_credit_limit_minor || 0) - (billing.customer_postpaid_used_minor || 0)), billing.currency || 'INR')} /></div>
          <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" alignItems={{ sm: 'center' }} gap={1} sx={{ mt: 1.5 }}><Typography variant="body2" color="text.secondary">Rate: {formatMoney(billing.rate_per_minute_minor || 0, billing.currency || 'INR')} per minute</Typography><Button variant="outlined" size="small" onClick={() => { const minor = billing.billing_mode === 'prepaid' ? billing.customer_prepaid_balance_minor : billing.customer_postpaid_credit_limit_minor; onAdjustBilling(((minor || 0) / 100).toFixed(2)) }}>Adjust balance / limit</Button></Stack>
        </> : <Alert severity="warning">{billing?.message || 'No billing plan is configured for this API customer.'}</Alert>}
      </Card>
      <Stack direction="row" alignItems="center" justifyContent="space-between" gap={1}>
        <Stack><Typography variant="h6" sx={{ fontWeight: 800 }}>Call history</Typography><Typography variant="body2" color="text.secondary">Recent calls placed with this API</Typography></Stack>
        <Button component={Link} to={`/api/${encodeURIComponent(apiKey.id)}/calls`} size="small" variant="outlined">View all · {callsTotal}</Button>
      </Stack>
      <Card className="surface-card" sx={{ p: 0, overflow: 'hidden' }}>
        {callsLoading && !calls.length ? <Stack alignItems="center" sx={{ py: 5 }}><CircularProgress size={24} /></Stack> : callsError ? <Alert severity="error" sx={{ m: 2 }}>{errorText(callsError)}</Alert> : calls.length ? <>
          <TableContainer className="table-surface" sx={{ overflowX: 'auto' }}><Table size="small" sx={{ minWidth: 850 }}><TableHead><TableRow>{['Started', 'Direction', 'Caller → recipient', 'Duration', 'Call status', 'Post-call', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{calls.map(call => <ApiCallRecord key={call.session_id} apiId={apiKey.id} call={call} />)}</TableBody></Table></TableContainer>
          <TablePagination component="div" count={callsTotal} page={callsPage} onPageChange={(_, page) => onCallsPageChange(page)} rowsPerPage={callsPageSize} onRowsPerPageChange={event => onCallsPageSizeChange(Number(event.target.value))} rowsPerPageOptions={[10, 25, 50]} labelRowsPerPage="Calls per page" showFirstButton showLastButton />
        </> : <Stack alignItems="center" sx={{ py: 4 }}><Typography variant="body2" color="text.secondary">No calls yet</Typography></Stack>}
      </Card>
    </Stack>}
    {apiKey && <Dialog open={configurationOpen} onClose={onCloseConfiguration} maxWidth="md" fullWidth>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1, fontFamily: 'Manrope', fontWeight: 800 }}><Stack><span>API configuration</span><Typography variant="body2" color="text.secondary">Phone numbers, agents, and runtime settings</Typography></Stack><Stack direction="row" alignItems="center" gap={1}><Chip size="small" variant="outlined" label={`Key · ${apiKey.key_prefix}`} /><IconButton aria-label="Close API configuration" onClick={onCloseConfiguration}><CloseRoundedIcon /></IconButton></Stack></DialogTitle>
      <DialogContent dividers><div className="api-kpi-grid"><UsageCard label="Assigned numbers" value={apiKey.assigned_numbers.map(number => number.phone_number).join(', ') || 'None'} /><UsageCard label="Recording" value={apiKey.recording_allowed ? 'Enabled' : 'Disabled'} /><UsageCard label="Calling agent" value={voiceAgent ? `${voiceAgent.name} · ${voiceAgent.provider}` : 'Not assigned'} /><UsageCard label="Calling model / location" value={`${apiKey.voice_agent_enabled === false ? 'Disabled' : apiKey.voice_model || voiceAgent?.model || 'Not configured'} · ${apiKey.voice_location || apiKey.vertex_location || voiceAgent?.location || '—'}`} /><UsageCard label="Post-call agent" value={postCallAgent ? `${postCallAgent.name} · ${postCallAgent.provider}` : 'Not assigned'} /><UsageCard label="Post-call model / location" value={`${apiKey.post_call_agent_enabled === false ? 'Disabled' : apiKey.post_call_model || postCallAgent?.model || 'Not configured'} · ${apiKey.post_call_location || postCallAgent?.location || '—'}`} /></div></DialogContent>
      <DialogActions><Button onClick={onCloseConfiguration}>Close</Button></DialogActions>
    </Dialog>}
  </>
}

function ApiCallHistoryPage({ api, loading, error, calls, callsTotal, callsPage, callsPageSize, onCallsPageChange, onCallsPageSizeChange, callsLoading, callsError }: {
  api: PlatformApiKey | null; loading: boolean; error: unknown; calls: ApiCallLog[]; callsTotal: number; callsPage: number; callsPageSize: number
  onCallsPageChange: (page: number) => void; onCallsPageSizeChange: (size: number) => void; callsLoading: boolean; callsError: unknown
}) {
  if (loading) return <Card className="surface-card" sx={{ p: 3 }}><CircularProgress size={24} /></Card>
  if (error) return <Alert severity="error">{errorText(error)}</Alert>
  if (!api) return <Alert severity="warning">This API could not be found. It may have been deleted.</Alert>
  return <Stack gap={1.5}><Stack direction="row" alignItems="baseline" justifyContent="space-between" gap={1}><Typography variant="h6" sx={{ fontWeight: 800 }}>Call history</Typography><Typography variant="body2" color="text.secondary">{callsTotal} {callsTotal === 1 ? 'call' : 'calls'} · {api.customer_name}</Typography></Stack><Card className="surface-card" sx={{ p: 0, overflow: 'hidden' }}>
    {callsLoading && !calls.length ? <Stack alignItems="center" sx={{ py: 6 }}><CircularProgress size={24} /></Stack> : callsError ? <Alert severity="error" sx={{ m: 2 }}>{errorText(callsError)}</Alert> : calls.length ? <>
      <TableContainer className="table-surface" sx={{ overflowX: 'auto' }}><Table size="small" sx={{ minWidth: 850 }}><TableHead><TableRow>{['Started', 'Direction', 'Caller → recipient', 'Duration', 'Call status', 'Post-call', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{calls.map(call => <ApiCallRecord key={call.session_id} apiId={api.id} call={call} />)}</TableBody></Table></TableContainer>
      <TablePagination component="div" count={callsTotal} page={callsPage} onPageChange={(_, page) => onCallsPageChange(page)} rowsPerPage={callsPageSize} onRowsPerPageChange={event => onCallsPageSizeChange(Number(event.target.value))} rowsPerPageOptions={[10, 25, 50, 100]} labelRowsPerPage="Calls per page" showFirstButton showLastButton sx={{ borderTop: 1, borderColor: 'divider', '.MuiTablePagination-toolbar': { px: { xs: 1.5, md: 2.5 }, flexWrap: 'wrap' } }} />
    </> : <Stack alignItems="center" textAlign="center" gap={0.75} sx={{ px: 3, py: 5 }}><Typography sx={{ fontWeight: 700 }}>No calls yet</Typography><Typography variant="body2" color="text.secondary">Calls placed with this API will appear here.</Typography></Stack>}
  </Card></Stack>
}

function ApiCallRecord({ apiId, call }: { apiId: string; call: ApiCallLog }) {
  const [expanded, setExpanded] = useState(false)
  const [sessionIdCopied, setSessionIdCopied] = useState(false)
  const [sessionIdCopyFailed, setSessionIdCopyFailed] = useState(false)
  const [requestRecording, setRequestRecording] = useState(false)
  const [recordingUrl, setRecordingUrl] = useState<string | null>(null)
  const detail = useQuery({ queryKey: ['platform-api-call-detail', apiId, call.session_id], queryFn: () => api.apiCallDetail(apiId, call.session_id), enabled: expanded, retry: false, refetchInterval: expanded && (call.status !== 'completed' || call.post_call_status === 'queued' || call.post_call_status === 'running') ? 4000 : false })
  const recording = useQuery({ queryKey: ['platform-api-call-recording', apiId, call.session_id], queryFn: () => api.apiCallRecording(apiId, call.session_id), enabled: expanded && requestRecording && Boolean(detail.data?.recording_available), retry: false })

  const copySessionId = () => {
    void navigator.clipboard.writeText(call.session_id).then(() => {
      setSessionIdCopied(true)
      setSessionIdCopyFailed(false)
    }).catch(() => {
      setSessionIdCopied(false)
      setSessionIdCopyFailed(true)
    })
  }

  useEffect(() => {
    if (!recording.data) return
    const url = URL.createObjectURL(recording.data)
    setRecordingUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [recording.data])

  return <Fragment>
    <TableRow hover>
      <TableCell className="table-cell"><Stack spacing={0.15}><Typography variant="body2" sx={{ fontWeight: 750 }}>{new Date(call.started_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</Typography><Typography variant="caption" color="text.secondary">{new Date(call.started_at).toLocaleDateString()}</Typography></Stack></TableCell>
      <TableCell className="table-cell" sx={{ textTransform: 'capitalize' }}>{call.direction || '—'}</TableCell>
      <TableCell className="table-cell"><Typography variant="body2">{call.caller || '—'} → {call.callee || '—'}</Typography><Typography variant="caption" color="text.secondary">{call.provider || 'unknown provider'} · <code>{call.session_id}</code></Typography></TableCell>
      <TableCell className="table-cell">{call.duration_seconds == null ? '—' : `${call.duration_seconds}s`}</TableCell>
      <TableCell className="table-cell"><StatusChip enabled={call.status === 'completed'} label={call.status} /></TableCell>
      <TableCell className="table-cell">{call.post_call_status ? <StatusChip enabled={call.post_call_status === 'completed'} label={call.post_call_status} /> : '—'}</TableCell>
      <TableCell className="table-cell"><Button size="small" onClick={() => setExpanded(true)}>Details</Button></TableCell>
    </TableRow>
    <Dialog open={expanded} onClose={() => setExpanded(false)} maxWidth="md" fullWidth>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1, fontFamily: 'Manrope', fontWeight: 800 }}><Stack><span>Call details</span><Stack direction="row" alignItems="center" gap={0.25}><Typography variant="body2" color="text.secondary">{new Date(call.started_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })} · {new Date(call.started_at).toLocaleDateString()} · {call.session_id}</Typography><Tooltip title={sessionIdCopyFailed ? 'Copy failed' : sessionIdCopied ? 'Copied' : 'Copy session ID'}><IconButton aria-label="Copy session ID" size="small" onClick={copySessionId} color={sessionIdCopyFailed ? 'error' : 'default'}>{sessionIdCopied ? <CheckCircleRoundedIcon fontSize="small" /> : <ContentCopyRoundedIcon fontSize="small" />}</IconButton></Tooltip></Stack></Stack><IconButton aria-label="Close call details" onClick={() => setExpanded(false)}><CloseRoundedIcon /></IconButton></DialogTitle>
      <DialogContent dividers>{detail.isLoading ? <Stack alignItems="center" sx={{ py: 4 }}><CircularProgress size={22} /></Stack> : detail.error ? <Alert severity="error">{errorText(detail.error)}</Alert> : detail.data && <Stack gap={1.5}>
        <Stack gap={0.75}>
          <Typography variant="subtitle2" sx={{ fontWeight: 800 }}>Recording</Typography>
          {detail.data.recording_available ? <Stack gap={1}>{recordingUrl ? <audio controls preload="none" src={recordingUrl} style={{ width: '100%' }}>Your browser does not support audio playback.</audio> : <Button size="small" variant="outlined" disabled={recording.isFetching} onClick={() => setRequestRecording(true)} sx={{ alignSelf: 'flex-start' }}>{recording.isFetching ? 'Loading recording…' : 'Load recording'}</Button>}{recording.error && <Alert severity="error">{errorText(recording.error)}</Alert>}</Stack> : <Typography variant="body2" color="text.secondary">{detail.data.recording_allowed ? 'No saved recording is available.' : 'Recording was disabled for this call.'}</Typography>}
        </Stack>
        <Card variant="outlined" sx={{ p: 1.5 }}><Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}><Typography variant="subtitle2" sx={{ fontWeight: 800 }}>Post-call result</Typography><CopyIconButton value={detail.data.post_call_result ? JSON.stringify(detail.data.post_call_result, null, 2) : ''} label="Copy post-call result" disabled={!detail.data.post_call_result} /></Stack>{detail.data.post_call_error && <Alert severity="error" sx={{ mb: 1 }}>{detail.data.post_call_error}</Alert>}{detail.data.post_call_result ? <><Alert severity={typeof detail.data.post_call_result.needs_audio_review !== 'boolean' ? 'info' : detail.data.post_call_result.needs_audio_review ? 'warning' : 'success'} sx={{ mb: 1 }}>{typeof detail.data.post_call_result.needs_audio_review !== 'boolean' ? 'Audio review status unavailable' : detail.data.post_call_result.needs_audio_review ? 'Audio review needed' : 'No audio review needed'}</Alert><pre style={{ whiteSpace: 'pre-wrap', overflowX: 'auto', maxHeight: 280, padding: 12, borderRadius: 8, background: 'rgba(120,130,150,.12)' }}>{JSON.stringify(detail.data.post_call_result, null, 2)}</pre></> : <Typography variant="body2" color="text.secondary">Result appears after post-call processing completes.</Typography>}</Card>
        <Card variant="outlined" sx={{ p: 1.5 }}><Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}><Typography variant="subtitle2" sx={{ fontWeight: 800 }}>Transcript</Typography><CopyIconButton value={detail.data.transcript || ''} label="Copy transcript" disabled={!detail.data.transcript} /></Stack>{detail.data.transcript ? <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 360, overflowY: 'auto', margin: 0, fontFamily: 'inherit' }}>{detail.data.transcript}</pre> : <Typography variant="body2" color="text.secondary">{detail.data.status === 'completed' ? 'Transcript is unavailable.' : 'Transcript is not ready yet.'}</Typography>}</Card>
      </Stack>}</DialogContent>
      <DialogActions><Button onClick={() => setExpanded(false)}>Close</Button></DialogActions>
    </Dialog>
  </Fragment>
}

function AgentsPage({ agents, loading, error, onEdit, onDelete }: { agents: AgentConfiguration[]; loading: boolean; error: unknown; onEdit: (agent: AgentConfiguration) => void; onDelete: (agent: AgentConfiguration) => void }) {
  const [page, setPage] = useState(0)
  const [rowsPerPage, setRowsPerPage] = useState(10)
  const visibleAgents = agents.slice(page * rowsPerPage, (page + 1) * rowsPerPage)
  return <>
    <Alert severity="info" sx={{ mt: 2 }}>Google Vertex AI is currently available for calling and post-call agents. API credentials and location overrides are configured on each API.</Alert>
    <Card className="table-surface" sx={{ mt: 1 }}>
      {loading ? <div style={{ textAlign: 'center', padding: 40 }}><CircularProgress size={24} /></div> : error ? <div className="error-box">{errorText(error)}</div> : agents.length ? <><TableContainer className="table-surface" sx={{ width: "100%", overflowX: "auto" }}><Table size="small" sx={{ minWidth: 980 }}><TableHead><TableRow>{['Agent', 'Type', 'Provider / model', 'Default location', 'Cost / minute', 'Auto hang-up', ''].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{visibleAgents.map(agent => <TableRow key={agent.id} hover><TableCell className="table-cell"><b>{agent.name}</b><TagChips tags={agent.tags || []} /></TableCell><TableCell className="table-cell">{agent.kind === 'calling' ? 'Calling' : 'Post-call'}</TableCell><TableCell className="table-cell">{agent.provider} · {agent.model}</TableCell><TableCell className="table-cell">{agent.location}</TableCell><TableCell className="table-cell">{formatMoney(agent.cost_per_minute_minor, agent.currency)}/min</TableCell><TableCell className="table-cell">{agent.kind === 'calling' ? <StatusChip enabled={agent.auto_hangup_when_complete} label={agent.auto_hangup_when_complete ? 'enabled' : 'disabled'} /> : '—'}</TableCell><TableCell className="table-cell"><Button size="small" onClick={() => onEdit(agent)}>Edit</Button><Button size="small" color="error" onClick={() => onDelete(agent)}>Delete</Button></TableCell></TableRow>)}</TableBody></Table></TableContainer><TablePagination component="div" count={agents.length} page={page} onPageChange={(_, value) => setPage(value)} rowsPerPage={rowsPerPage} onRowsPerPageChange={event => { setRowsPerPage(Number(event.target.value)); setPage(0) }} rowsPerPageOptions={[10, 25, 50]} /></> : <div className="empty-state"><div className="agent-avatar" style={{ margin: '0 auto 14px' }}><AutoAwesomeRoundedIcon /></div><b style={{ color: '#36425a', fontSize: 14 }}>No agents configured</b><div style={{ fontSize: 12, marginTop: 6 }}>Create calling or post-call agents first, then assign them to an API.</div></div>}
    </Card>
  </>
}

function ApiTemplatesSection({ apiId, templatesOpen, onCloseTemplates }: { apiId: string; templatesOpen: boolean; onCloseTemplates: () => void }) {
  const queryClient = useQueryClient()
  const { data, isLoading, error } = useQuery({
    queryKey: ['api-agent-templates', apiId],
    queryFn: () => api.apiAgentTemplates(apiId),
    enabled: Boolean(apiId) && templatesOpen,
  })

  const [editing, setEditing] = useState<AgentPromptTemplate | null>(null)
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [kind, setKind] = useState<'calling' | 'post_call'>('calling')
  const [instructions, setInstructions] = useState('')
  const [tools, setTools] = useState<string[]>([])
  const [variablesJson, setVariablesJson] = useState('{}')
  const [settingsJson, setSettingsJson] = useState('{\n  "time_zone": "UTC",\n  "time_format": "%Y-%m-%dT%H:%M:%S%z"\n}')
  const [failureWebhookUrl, setFailureWebhookUrl] = useState('')
  const [failureWebhookPayloadJson, setFailureWebhookPayloadJson] = useState('{}')
  const [isDefault, setIsDefault] = useState(false)
  const [formError, setFormError] = useState('')
  const [templateDetails, setTemplateDetails] = useState<AgentPromptTemplate | null>(null)
  const [helpOpen, setHelpOpen] = useState(false)
  const [templatePage, setTemplatePage] = useState(0)
  const [templatesPerPage, setTemplatesPerPage] = useState(6)

  const saveMutation = useMutation({
    mutationFn: (value: Omit<AgentPromptTemplate, 'id' | 'revision' | 'enabled'> & { is_default?: boolean }) =>
      editing
        ? api.updateApiAgentTemplate(apiId, editing.id, value)
        : api.createApiAgentTemplate(apiId, value),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['api-agent-templates', apiId] })
      setOpen(false)
      setFormError('')
    },
    onError: err => setFormError(errorText(err)),
  })

  const deleteMutation = useMutation({
    mutationFn: (templateId: string) => api.deleteApiAgentTemplate(apiId, templateId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['api-agent-templates', apiId] })
    },
    onError: err => alert(errorText(err)),
  })

  const setDefaultMutation = useMutation({
    mutationFn: (templateId: string) => api.setDefaultApiAgentTemplate(apiId, templateId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['api-agent-templates', apiId] })
    },
    onError: err => alert(errorText(err)),
  })

  const openNew = () => {
    setEditing(null)
    setName('')
    setKind('calling')
    setInstructions('')
    setTools([])
    setVariablesJson('{}')
    setSettingsJson('{\n  "time_zone": "UTC",\n  "time_format": "%Y-%m-%dT%H:%M:%S%z"\n}')
    setFailureWebhookUrl('')
    setFailureWebhookPayloadJson('{}')
    setIsDefault(false)
    setFormError('')
    setOpen(true)
  }

  const openEdit = (item: AgentPromptTemplate) => {
    setEditing(item)
    setName(item.name)
    setKind(item.kind)
    setInstructions(item.instructions)
    setTools(item.tools.map(tool => tool.name))
    setVariablesJson(JSON.stringify(item.variables, null, 2))
    const { failure_webhook_url, failure_webhook_payload, ...templateSettings } = item.settings
    setSettingsJson(JSON.stringify(templateSettings, null, 2))
    setFailureWebhookUrl(typeof failure_webhook_url === 'string' ? failure_webhook_url : '')
    setFailureWebhookPayloadJson(JSON.stringify(failure_webhook_payload || {}, null, 2))
    setIsDefault(Boolean(item.is_default))
    setFormError('')
    setOpen(true)
  }

  const save = () => {
    try {
      const variables = JSON.parse(variablesJson)
      const settings = JSON.parse(settingsJson)
      if (!variables || Array.isArray(variables) || typeof variables !== 'object') {
        throw new Error('Variables must be a JSON object.')
      }
      if (!settings || Array.isArray(settings) || typeof settings !== 'object') {
        throw new Error('Settings must be a JSON object.')
      }
      if (kind === 'post_call') {
        let customFailurePayload: unknown
        try { customFailurePayload = JSON.parse(failureWebhookPayloadJson || '{}') }
        catch { throw new Error('Failure fallback payload must be valid JSON.') }
        if (!customFailurePayload || Array.isArray(customFailurePayload) || typeof customFailurePayload !== 'object') {
          throw new Error('Failure fallback payload must be a JSON object.')
        }
        if (failureWebhookUrl.trim()) settings.failure_webhook_url = failureWebhookUrl.trim()
        else delete settings.failure_webhook_url
        if (Object.keys(customFailurePayload).length) settings.failure_webhook_payload = customFailurePayload
        else delete settings.failure_webhook_payload
      }
      saveMutation.mutate({
        name: name.trim(),
        kind,
        instructions: instructions.trim(),
        tools: tools.map(tool => ({ type: 'builtin', name: tool })),
        variables,
        settings,
        is_default: isDefault,
      })
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Check the JSON fields.')
    }
  }

  const allTemplates = [
    ...(data?.calling || []),
    ...(data?.post_call || []),
  ]
  const allowedTools = kind === 'calling' ? ['end_call'] : ['calculator', 'time']

  return (
    <>
      <Dialog open={templatesOpen} onClose={onCloseTemplates} maxWidth="md" fullWidth>
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1, fontFamily: 'Manrope', fontWeight: 800 }}>Prompt templates<IconButton aria-label="Close prompt templates" onClick={onCloseTemplates}><CloseRoundedIcon /></IconButton></DialogTitle>
        <DialogContent dividers>
      <Card className="surface-card" sx={{ p: { xs: 2, md: 3 } }}>
        <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" alignItems={{ xs: 'stretch', sm: 'center' }} gap={1.5} sx={{ mb: 2 }}>
          <div>
            <Stack direction="row" alignItems="center" gap={1}>
              <Typography variant="h6" sx={{ fontWeight: 800 }}>Prompt templates</Typography>
              <Chip size="small" label={allTemplates.length} />
              <Tooltip title="Template notes"><IconButton size="small" aria-label="Prompt template notes" onClick={() => setHelpOpen(true)}><InfoOutlinedIcon fontSize="small" /></IconButton></Tooltip>
            </Stack>
            <Typography variant="body2" color="text.secondary">Open a template icon to review its prompt and settings.</Typography>
          </div>
          <Button variant="contained" startIcon={<AddRoundedIcon />} onClick={openNew}>
            Create template
          </Button>
        </Stack>

        {isLoading ? <Stack alignItems="center" sx={{ py: 4 }}><CircularProgress size={24} /></Stack> : error ? <Alert severity="error">{errorText(error)}</Alert> : allTemplates.length ? <>
          <Stack gap={1.25}>
            {allTemplates.slice(templatePage * templatesPerPage, (templatePage + 1) * templatesPerPage).map(item => {
              const isDefaultTemplate = (item.kind === 'calling' && data?.default_calling_id === item.id) || (item.kind === 'post_call' && data?.default_post_call_id === item.id) || Boolean(item.is_default)
              return <Card key={item.id} variant="outlined" sx={{ display: 'flex', alignItems: 'center', gap: 1.5, p: 1.25, borderRadius: 2 }}>
                <Tooltip title={`View ${item.name} details`}><IconButton aria-label={`View ${item.name} details`} onClick={() => setTemplateDetails(item)} sx={{ bgcolor: item.kind === 'calling' ? 'primary.50' : 'secondary.50', color: item.kind === 'calling' ? 'primary.main' : 'secondary.main' }}><DescriptionRoundedIcon /></IconButton></Tooltip>
                <Stack sx={{ minWidth: 0, flex: 1 }}><Typography variant="subtitle2" noWrap sx={{ fontWeight: 750 }}>{item.name}</Typography><Stack direction="row" gap={0.75} alignItems="center"><Chip size="small" label={item.kind === 'calling' ? 'Calling' : 'Post-call'} variant="outlined" color={item.kind === 'calling' ? 'primary' : 'secondary'} /><Typography variant="caption" color="text.secondary">v{item.revision} · {item.tools.length ? item.tools.map(tool => tool.name).join(', ') : 'No tools'}</Typography></Stack></Stack>
                {isDefaultTemplate ? <Chip size="small" color="success" label="Default" /> : <Button size="small" onClick={() => setDefaultMutation.mutate(item.id)} disabled={setDefaultMutation.isPending}>Set default</Button>}
                <Button size="small" onClick={() => openEdit(item)}>Edit</Button><IconButton aria-label={`Delete ${item.name}`} color="error" size="small" disabled={deleteMutation.isPending} onClick={() => { if (window.confirm(`Delete template “${item.name}” from this API?`)) deleteMutation.mutate(item.id) }}><CloseRoundedIcon fontSize="small" /></IconButton>
              </Card>
            })}
          </Stack>
          <TablePagination component="div" count={allTemplates.length} page={templatePage} onPageChange={(_, page) => setTemplatePage(page)} rowsPerPage={templatesPerPage} onRowsPerPageChange={event => { setTemplatesPerPage(Number(event.target.value)); setTemplatePage(0) }} rowsPerPageOptions={[6, 12, 24]} labelRowsPerPage="Templates per page" />
        </> : <Stack alignItems="center" textAlign="center" gap={0.75} sx={{ px: 3, py: 4 }}><Typography sx={{ fontWeight: 700 }}>No templates owned by this API</Typography><Typography variant="body2" color="text.secondary">Create a calling or post-call prompt template for this API to get started.</Typography><Button variant="outlined" startIcon={<AddRoundedIcon />} onClick={openNew} sx={{ mt: 1 }}>Create template</Button></Stack>}
      </Card>
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(templateDetails)} onClose={() => setTemplateDetails(null)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 1 }}><Stack><span>{templateDetails?.name}</span><Typography variant="body2" color="text.secondary">{templateDetails?.kind === 'calling' ? 'Calling' : 'Post-call'} template · revision {templateDetails?.revision}</Typography></Stack><IconButton aria-label="Close template details" onClick={() => setTemplateDetails(null)}><CloseRoundedIcon /></IconButton></DialogTitle>
        <DialogContent dividers><Stack gap={1.5}><Typography variant="subtitle2">Instructions</Typography><Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>{templateDetails?.instructions}</Typography><Typography variant="subtitle2">Tools</Typography><Typography variant="body2">{templateDetails?.tools.map(tool => tool.name).join(', ') || 'None'}</Typography><Typography variant="subtitle2">Variables</Typography><pre style={{ margin: 0, maxHeight: 220, overflow: 'auto', whiteSpace: 'pre-wrap' }}>{JSON.stringify(templateDetails?.variables || {}, null, 2)}</pre><Typography variant="subtitle2">Settings</Typography><pre style={{ margin: 0, maxHeight: 220, overflow: 'auto', whiteSpace: 'pre-wrap' }}>{JSON.stringify(templateDetails?.settings || {}, null, 2)}</pre></Stack></DialogContent>
      </Dialog>
      <Dialog open={helpOpen} onClose={() => setHelpOpen(false)} maxWidth="sm" fullWidth><DialogTitle>Prompt template notes</DialogTitle><DialogContent dividers><Typography variant="body2">When a call has no transcript, post-call AI is skipped and a signed <code>call.post_call.failed</code> event is sent. A post-call template can override the API fallback URL and add a custom payload. Otherwise, API-level fallback settings are used.</Typography></DialogContent><DialogActions><Button onClick={() => setHelpOpen(false)}>Close</Button></DialogActions></Dialog>

      <Dialog open={open} onClose={() => setOpen(false)} maxWidth="md" fullWidth>
        <DialogTitle sx={{ fontFamily: 'Manrope', fontWeight: 800 }}>
          {editing ? 'Edit template' : 'Create template for this API'}
        </DialogTitle>
        <DialogContent>
          <Stack gap={2} sx={{ mt: 1 }}>
            {formError && <Alert severity="error">{formError}</Alert>}
            <TextField label="Template name" value={name} onChange={event => setName(event.target.value)} required fullWidth />
            <FormControl fullWidth disabled={Boolean(editing)}>
              <InputLabel id="api-template-kind">Agent type</InputLabel>
              <Select
                labelId="api-template-kind"
                label="Agent type"
                value={kind}
                onChange={event => {
                  const next = event.target.value as 'calling' | 'post_call'
                  setKind(next)
                  setTools([])
                  if (next === 'post_call') {
                    setSettingsJson('{\n  "time_zone": "UTC",\n  "time_format": "%Y-%m-%dT%H:%M:%S%z"\n}')
                  } else {
                    setSettingsJson('{}')
                    setFailureWebhookUrl('')
                    setFailureWebhookPayloadJson('{}')
                  }
                }}
              >
                <MenuItem value="calling">Calling agent</MenuItem>
                <MenuItem value="post_call">Post-call agent</MenuItem>
              </Select>
            </FormControl>
            <TextField
              label="Instructions"
              value={instructions}
              onChange={event => setInstructions(event.target.value)}
              multiline
              minRows={5}
              required
              fullWidth
              helperText="Use placeholders like {{patient_name}} and declare them in Variables below."
            />
            <Stack direction="row" gap={1} flexWrap="wrap">
              {allowedTools.map(tool => (
                <Chip
                  key={tool}
                  clickable
                  color={tools.includes(tool) ? 'primary' : 'default'}
                  variant={tools.includes(tool) ? 'filled' : 'outlined'}
                  label={tool}
                  onClick={() => setTools(curr => curr.includes(tool) ? curr.filter(i => i !== tool) : [...curr, tool])}
                />
              ))}
            </Stack>
            <TextField
              label="Variables (JSON object)"
              value={variablesJson}
              onChange={event => setVariablesJson(event.target.value)}
              multiline
              minRows={4}
              fullWidth
              helperText={'Example: { "patient_name": { "type": "string", "required": true } }'}
            />
            <TextField
              label="Template settings (JSON object)"
              value={settingsJson}
              onChange={event => setSettingsJson(event.target.value)}
              multiline
              minRows={3}
              fullWidth
              helperText={kind === 'post_call' ? 'Optional defaults: time_zone, time_format, output_schema. Failure fallback settings are configured below.' : 'Optional: language (for example ta-IN), auto_hangup_when_complete, and completion_requirements. Automatic hang-up requires always/when field rules or live_agent.context.mandatory_fields.'}
            />
            {kind === 'post_call' && <>
              <Alert severity="info">These fallback settings override this API’s default only for calls using this post-call template. If the URL is blank, the API failure webhook is used.</Alert>
              <TextField label="Template failure webhook URL (optional)" type="url" value={failureWebhookUrl} onChange={event => setFailureWebhookUrl(event.target.value)} fullWidth placeholder="https://service.example.com/call-failures" />
              <TextField label="Template custom failure payload (JSON object)" value={failureWebhookPayloadJson} onChange={event => setFailureWebhookPayloadJson(event.target.value)} multiline minRows={3} fullWidth helperText={'Added under "custom_payload"; the signed event still includes call ID and failure code. Static JSON only.'} />
            </>}
            {!editing && (
              <FormControlLabel
                control={<Switch checked={isDefault} onChange={event => setIsDefault(event.target.checked)} />}
                label={`Set as default ${kind === 'calling' ? 'calling' : 'post-call'} template for this API`}
              />
            )}
          </Stack>
        </DialogContent>
        <DialogActions sx={{ p: 2.5 }}>
          <Button onClick={() => setOpen(false)}>Cancel</Button>
          <Button variant="contained" disabled={saveMutation.isPending || !name.trim() || !instructions.trim()} onClick={save}>
            {saveMutation.isPending ? 'Saving…' : 'Save template'}
          </Button>
        </DialogActions>
      </Dialog>
    </>
  )
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
  templates?: ApiAgentTemplates; templatesLoading: boolean
  callingTemplateId: string; onCallingTemplateChange: (id: string) => void
  postCallTemplateId: string; onPostCallTemplateChange: (id: string) => void
  onStart: (template?: SimulationTemplate) => void
  onApplyTemplate: (template: SimulationTemplate) => void
  submitting: boolean
}) {
  const selected = props.keys.find(key => key.id === props.selectedApiId)
  const assignedNumbers = selected?.assigned_numbers || []
  const [setupOpen, setSetupOpen] = useState(false)
  const [setupView, setSetupView] = useState<'form' | 'json'>('form')
  const [templates, setTemplates] = useState<SimulationTemplate[]>(() => {
    try {
      const parsed: unknown = JSON.parse(window.localStorage.getItem('chiefvoice.simulation-templates') || '[]')
      return Array.isArray(parsed) ? parsed as SimulationTemplate[] : []
    } catch { return [] }
  })
  const [templatePage, setTemplatePage] = useState(0)
  const [templatesPerPage, setTemplatesPerPage] = useState(10)
  const [templateDialogOpen, setTemplateDialogOpen] = useState(false)
  const [templateName, setTemplateName] = useState('')
  const [editingTemplateId, setEditingTemplateId] = useState('')
  const [templateNotice, setTemplateNotice] = useState('')
  const requestJson = (() => {
    if (!selected) return '{}'
    try {
      const liveContext = JSON.parse(props.liveContext || '{}')
      const postCallContext = JSON.parse(props.postCallContext || '{}')
      const outputSchema = JSON.parse(props.outputSchema || '{}')
      if (!liveContext || Array.isArray(liveContext) || typeof liveContext !== 'object') return '{\n  "error": "Live-call context must be a JSON object"\n}'
      if (!postCallContext || Array.isArray(postCallContext) || typeof postCallContext !== 'object') return '{\n  "error": "Post-call context must be a JSON object"\n}'
      if (!outputSchema || Array.isArray(outputSchema) || typeof outputSchema !== 'object') return '{\n  "error": "Output schema must be a JSON object"\n}'
      const body: Record<string, unknown> = {
        channel: 'phone',
        destination: props.destination.trim(),
        caller_id: props.callerId.split('|')[1] || '',
        live_agent: { agent_id: props.profileId || null, template_id: props.callingTemplateId || null, context: liveContext },
        recording_allowed: props.recording,
      }
      if (selected.post_call_agent_enabled) body.post_call = {
        template_id: props.postCallTemplateId || null,
        context: postCallContext,
        output_schema: outputSchema,
        ...(props.tools.length ? { tools: props.tools.map(name => ({ type: 'builtin', name })) } : {}),
        time_zone: props.timeZone,
        time_format: props.timeFormat,
        include_live_agent_context: props.includeLiveContext,
      }
      return JSON.stringify(body, null, 2)
    } catch (error) {
      return JSON.stringify({ error: error instanceof Error ? error.message : 'Check the JSON fields.' }, null, 2)
    }
  })()
  useEffect(() => {
    try { window.localStorage.setItem('chiefvoice.simulation-templates', JSON.stringify(templates)) } catch { setTemplateNotice('Could not save templates in this browser storage.') }
  }, [templates])
  useEffect(() => {
    const openSetup = () => { setSetupView('form'); setSetupOpen(true) }
    window.addEventListener('chiefvoice-open-call-setup', openSetup)
    return () => window.removeEventListener('chiefvoice-open-call-setup', openSetup)
  }, [])
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
      callingTemplateId: props.callingTemplateId, postCallTemplateId: props.postCallTemplateId,
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
    <Alert severity="warning" sx={{ mt: 2 }}>This places a real outbound call. The selected API’s billing plan, agent costs, and recording policy apply.</Alert>
    <Card className="surface-card" sx={{ p: { xs: 1.5, md: 2 }, mt: 2 }}>
      <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" alignItems={{ xs: 'stretch', sm: 'center' }} gap={1.5}>
        <div><Typography variant="h6" sx={{ fontWeight: 800 }}>One-click simulation templates</Typography><Typography variant="body2" color="text.secondary">Saved test configurations for quick repeat calls.</Typography></div>
        <Button variant="outlined" onClick={openTemplateDialog} disabled={!canSaveTemplate}>Save current setup as template</Button>
      </Stack>
      {templateNotice && <Alert severity="info" sx={{ mt: 1.5 }} onClose={() => setTemplateNotice('')}>{templateNotice}</Alert>}
      {templates.length ? <><TableContainer className="table-surface" sx={{ mt: 1.5, overflowX: 'auto' }}><Table size="small" sx={{ minWidth: 800 }}><TableHead><TableRow>{['Template', 'API', 'Destination', 'Caller ID', 'Actions'].map(label => <TableCell className="table-head" key={label}>{label}</TableCell>)}</TableRow></TableHead><TableBody>{templates.slice(templatePage * templatesPerPage, (templatePage + 1) * templatesPerPage).map(template => {
        const templateApi = props.keys.find(key => key.id === template.apiId)
        const isUsable = Boolean(templateApi && !templateApi.revoked_at && templateApi.voice_agent_enabled && templateApi.assigned_numbers.some(number => `${number.provider}|${number.phone_number}` === template.callerId))
        const caller = templateApi?.assigned_numbers.find(number => `${number.provider}|${number.phone_number}` === template.callerId)?.phone_number
        return <TableRow key={template.id} hover><TableCell className="table-cell" sx={{ fontWeight: 700 }}>{template.name}</TableCell><TableCell className="table-cell">{templateApi ? `${templateApi.customer_name} · ${templateApi.name}` : 'API unavailable'}</TableCell><TableCell className="table-cell">{template.destination}</TableCell><TableCell className="table-cell">{caller || 'Unavailable'}</TableCell><TableCell className="table-cell"><Stack direction="row" gap={0.5} flexWrap="wrap"><Button size="small" onClick={() => { loadTemplateForEdit(template); setSetupOpen(true) }}>Edit</Button><Button size="small" color="error" onClick={() => deleteTemplate(template)}>Delete</Button><Button size="small" variant="contained" startIcon={<ScienceRoundedIcon />} disabled={!isUsable || props.submitting} onClick={() => props.onStart(template)}>{props.submitting ? 'Sending…' : 'Send'}</Button></Stack></TableCell></TableRow>
      })}</TableBody></Table></TableContainer><TablePagination component="div" count={templates.length} page={templatePage} onPageChange={(_, page) => setTemplatePage(page)} rowsPerPage={templatesPerPage} onRowsPerPageChange={event => { setTemplatesPerPage(Number(event.target.value)); setTemplatePage(0) }} rowsPerPageOptions={[10, 25, 50]} labelRowsPerPage="Templates per page" /></> : <div className="empty-state" style={{ padding: '32px 16px' }}>No saved templates yet.</div>}
    </Card>
    <Dialog open={setupOpen} onClose={() => setSetupOpen(false)} maxWidth="md" fullWidth scroll="paper">
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1, fontFamily: 'Manrope', fontWeight: 800 }}>
        Call setup
        <IconButton aria-label="Close call setup" onClick={() => setSetupOpen(false)}><CloseRoundedIcon /></IconButton>
      </DialogTitle>
      <Tabs value={setupView} onChange={(_, value) => setSetupView(value)} sx={{ px: 2, borderBottom: 1, borderColor: 'divider' }}>
        <Tab value="form" label="Form view" />
        <Tab value="json" label="JSON view" />
      </Tabs>
      <DialogContent dividers>
        {setupView === 'json' ? <Stack gap={1.5}>
          <Typography variant="body2" color="text.secondary">This is the request body sent by the current form settings.</Typography>
          <CopyableCode value={requestJson} minRows={Math.max(10, Math.min(30, requestJson.split('\n').length))} />
        </Stack> : <>
      {props.loading ? <CircularProgress size={24} /> : props.error ? <Alert severity="error">{errorText(props.error)}</Alert> : !props.keys.length ? <div className="empty-state">Create an API and assign a phone number before starting a simulation.</div> : <Stack gap={2.5}>
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
        <FormControl fullWidth disabled={!selected}><InputLabel id="simulation-live-template">Calling template</InputLabel><Select labelId="simulation-live-template" label="Calling template" value={props.callingTemplateId} onChange={event => props.onCallingTemplateChange(event.target.value)}><MenuItem value="">API default template</MenuItem>{(props.templates?.calling || []).map(item => <MenuItem key={item.id} value={item.id}>{item.name} · v{item.revision}</MenuItem>)}</Select></FormControl>
        <FormControl fullWidth disabled={!selected}><InputLabel id="simulation-profile-label">Agent profile (optional)</InputLabel><Select labelId="simulation-profile-label" label="Agent profile (optional)" value={props.profileId} onChange={event => props.onProfileChange(event.target.value)}><MenuItem value="">API default voice</MenuItem>{props.profiles.map(profile => <MenuItem key={profile.id} value={profile.id}>{profile.name} · {profile.language} · {profile.voice}</MenuItem>)}</Select></FormControl>
        {props.profilesLoading && <Typography variant="caption" color="text.secondary">Loading profiles owned by this API customer…</Typography>}
        <TextField fullWidth multiline minRows={3} label="Live-call context (JSON object)" value={props.liveContext} onChange={event => props.onLiveContextChange(event.target.value)} helperText="Values matching template placeholders are rendered; all context remains available as reference data." />
        <Typography variant="h6" sx={{ fontWeight: 800, mt: 1 }}>Post-call agent</Typography>
        {!selected?.post_call_agent_enabled ? <Alert severity="info">Post-call analysis is disabled for this API. Enable a post-call agent in API settings to test extraction.</Alert> : <>
          <FormControl fullWidth><InputLabel id="simulation-post-template">Post-call template</InputLabel><Select labelId="simulation-post-template" label="Post-call template" value={props.postCallTemplateId} onChange={event => props.onPostCallTemplateChange(event.target.value)}><MenuItem value="">API default template</MenuItem>{(props.templates?.post_call || []).map(item => <MenuItem key={item.id} value={item.id}>{item.name} · v{item.revision}</MenuItem>)}</Select></FormControl>
          <TextField fullWidth multiline minRows={3} label="Post-call context (JSON object)" value={props.postCallContext} onChange={event => props.onPostCallContextChange(event.target.value)} helperText="Task instructions live in the selected template. Context supplies values for placeholders and structured reference data." />
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
      </Stack>}
        </>}
      </DialogContent>
      <DialogActions sx={{ p: 2 }}>
        <Button onClick={() => setSetupOpen(false)}>Close</Button>
        {editingTemplateId && <Button variant="outlined" onClick={() => setTemplateDialogOpen(true)} disabled={!canSaveTemplate}>Update template</Button>}
        <Button variant="contained" startIcon={<ScienceRoundedIcon />} onClick={() => props.onStart()} disabled={!selected || !selected.voice_agent_enabled || !props.callerId || !props.destination.trim() || props.submitting}>{props.submitting ? 'Starting call…' : 'Start test call'}</Button>
      </DialogActions>
    </Dialog>
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
  const [search, setSearch] = useState('')
  const [activeDoc, setActiveDoc] = useState('getting-started')
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const docsNavigation = [
    { id: 'getting-started', label: 'Getting started', group: 'Guides' },
    { id: 'requirements', label: 'Before you start', group: 'Guides' },
    { id: 'start-call', label: 'Place an outbound call', group: 'Guides' },
    { id: 'call-response', label: 'Call response', group: 'Guides' },
    { id: 'call-status', label: 'Status and results', group: 'Guides' },
    { id: 'transcripts', label: 'Fetch a transcript', group: 'Calls' },
    { id: 'webhooks', label: 'Call outcome webhooks', group: 'Events' },
    { id: 'recordings', label: 'Recordings', group: 'Calls' },
  ]
  const filteredNavigation = docsNavigation.filter(item => item.label.toLowerCase().includes(search.toLowerCase()))
  const goToSection = (id: string) => {
    setActiveDoc(id)
    document.getElementById(`docs-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
  useEffect(() => {
    const article = document.querySelector<HTMLElement>('.docs-article')
    if (!article) return
    const syncActiveSection = () => {
      const readingLine = article.getBoundingClientRect().top + 110
      let current = docsNavigation[0]?.id || 'getting-started'
      for (const item of docsNavigation) {
        const section = document.getElementById(`docs-${item.id}`)
        if (section && section.getBoundingClientRect().top <= readingLine) current = item.id
      }
      if (article.scrollTop + article.clientHeight >= article.scrollHeight - 2) {
        current = docsNavigation[docsNavigation.length - 1]?.id || current
      }
      setActiveDoc(value => value === current ? value : current)
    }
    article.addEventListener('scroll', syncActiveSection, { passive: true })
    syncActiveSection()
    return () => article.removeEventListener('scroll', syncActiveSection)
  }, [])
  const callExample = `curl -X POST "$CHIEFVOICE_URL/v1/calls/outbound" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: appointment-avery-2026-11-18" \\
  -d '{
    "channel": "phone",
    "destination": "+919876543210",
    "caller_id": "+919876500000",
    "live_agent": {
      "template_id": "YOUR_CALLING_TEMPLATE_ID",
      "context": {
        "patient_name": "Avery",
        "appointment": "tomorrow at 10:30 AM"
      }
    },
    "post_call": {
      "template_id": "YOUR_POST_CALL_TEMPLATE_ID",
      "context": {"appointment": "tomorrow at 10:30 AM"},
      "output_schema": {
        "type": "object",
        "properties": {
          "confirmed": {"type": "boolean"},
          "outcome": {"type": "string", "enum": ["confirmed", "reschedule_requested", "unknown"]}
        },
        "required": ["confirmed", "outcome"],
        "additionalProperties": false
      },
      "include_live_agent_context": true
    }
  }'`
  const statusExample = `curl "$CHIEFVOICE_URL/v1/calls/$SESSION_ID" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY"`
  const resultExample = `curl "$CHIEFVOICE_URL/v1/calls/$SESSION_ID/post-call" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY"`
  const transcriptExample = `curl "$CHIEFVOICE_URL/v1/calls/$SESSION_ID/transcript" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY" \\
  -H "Accept: application/json"`
  const webhookVerifyExample = `import hashlib
import hmac

raw_body = request.get_data()
timestamp = request.headers["X-Chiefvoice-Timestamp"]
event_id = request.headers["X-Chiefvoice-Event-Id"]
provided = request.headers["X-Chiefvoice-Signature"]
signed = timestamp.encode() + b"." + event_id.encode() + b"." + raw_body
expected = "sha256=" + hmac.new(
    WEBHOOK_SIGNING_SECRET.encode(), signed, hashlib.sha256
).hexdigest()
if not hmac.compare_digest(provided, expected):
    return "invalid signature", 401

event = request.get_json()
if already_processed(event_id):
    return "already processed", 200
process_failure(event)
mark_processed(event_id)
return "ok", 200`
  const recordingExample = `curl "$CHIEFVOICE_URL/v1/calls/$SESSION_ID/recording" \\
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY" \\
  --output call-recording.wav`
  return <div className={`docs-workspace ${sidebarCollapsed ? 'docs-collapsed' : ''}`}>
    <aside className="docs-sidebar">
      <div className="docs-brand"><span className="docs-brand-mark"><HubRoundedIcon fontSize="small" /></span><span className="docs-brand-name">Chiefvoice</span>{!sidebarCollapsed && <Chip size="small" label="API" />}<Tooltip title={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'} placement="right"><IconButton className="docs-collapse-button" size="small" aria-label={sidebarCollapsed ? 'Expand documentation sidebar' : 'Collapse documentation sidebar'} onClick={() => setSidebarCollapsed(value => !value)}>{sidebarCollapsed ? <ChevronRightRoundedIcon fontSize="small" /> : <ChevronLeftRoundedIcon fontSize="small" />}</IconButton></Tooltip></div>
      {!sidebarCollapsed && <TextField size="small" fullWidth placeholder="Search documentation" value={search} onChange={event => setSearch(event.target.value)} InputProps={{ startAdornment: <InputAdornment position="start"><SearchRoundedIcon fontSize="small" /></InputAdornment> }} />}
      {['Guides', 'Events', 'Calls'].map(group => {
        const items = filteredNavigation.filter(item => item.group === group)
        return items.length ? <div className="docs-nav-group" key={group}>{!sidebarCollapsed && <Typography className="docs-nav-heading">{group}</Typography>}{items.map(item => <Tooltip key={item.id} title={sidebarCollapsed ? item.label : ''} placement="right"><Button aria-label={item.label} title={sidebarCollapsed ? item.label : undefined} className={`docs-nav-item ${activeDoc === item.id ? 'active' : ''}`} onClick={() => goToSection(item.id)}>{sidebarCollapsed ? <span className="docs-nav-initial">{item.label.slice(0, 1)}</span> : item.label}</Button></Tooltip>)}</div> : null
      })}
      {!sidebarCollapsed && !filteredNavigation.length && <Typography variant="body2" color="text.secondary" sx={{ p: 1.5 }}>No matching pages.</Typography>}
    </aside>
    <main className="docs-article">
      <div className="docs-breadcrumb">Developer API <span>›</span> Getting started</div>
      <div className="docs-article-heading" id="docs-getting-started"><Chip size="small" variant="outlined" label="GUIDE" /><Typography variant="h3">Make your first call</Typography><Typography variant="body1" color="text.secondary">Use your API key to place a phone call, follow its status, and retrieve the post-call result.</Typography></div>
      <section className="docs-section" id="docs-requirements"><Typography variant="h5">Before you start</Typography><Typography>Have these items ready:</Typography><ul><li>The full API key shown when the API was created.</li><li>An outbound-enabled phone number assigned to the API.</li><li>Calling and post-call templates assigned to the API.</li></ul><Alert severity="info" sx={{ mt: 1.5 }}>Set <code>CHIEFVOICE_URL</code> to your API base URL and keep <code>CHIEFVOICE_API_KEY</code> secret.</Alert></section>
      <section className="docs-section" id="docs-start-call"><div className="docs-section-title"><div><Typography variant="h5">Place an outbound call</Typography><Typography color="text.secondary">Example: confirm an appointment and extract the outcome.</Typography></div><Chip className="docs-method" label="POST  /v1/calls/outbound" /></div><CopyableCode value={callExample} minRows={26} /><Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>Replace the example phone numbers and template IDs with values assigned to your API. Use a unique <code>Idempotency-Key</code> for each call.</Typography></section>
      <section className="docs-section" id="docs-call-response"><Typography variant="h5">Read the response</Typography><Typography color="text.secondary" sx={{ mb: 1.5 }}>The API returns <code>202 Accepted</code> with a session ID while the call runs.</Typography><pre className="docs-response">{`{\n  "session_id": "<returned-session-id>",\n  "provider": "vobiz",\n  "channel": "phone",\n  "direction": "outbound",\n  "status": "starting"\n}`}</pre></section>
      <section className="docs-section" id="docs-call-status"><Typography variant="h5">Check status and results</Typography><Typography color="text.secondary" sx={{ mb: 2 }}>Use the returned session ID to check progress and retrieve structured analysis.</Typography><div className="docs-endpoint"><Typography variant="subtitle2">Call status</Typography><Chip size="small" label="GET" color="success" /><CopyableCode value={statusExample} minRows={2} /></div><div className="docs-endpoint"><Typography variant="subtitle2">Post-call result</Typography><Chip size="small" label="GET" color="success" /><CopyableCode value={resultExample} minRows={2} /></div></section>
      <section className="docs-section" id="docs-transcripts"><div className="docs-section-title"><div><Typography variant="h5">Fetch a transcript</Typography><Typography color="text.secondary">Retrieve the transcript for a call you own.</Typography></div><Chip className="docs-method" label="GET  /v1/calls/{session_id}/transcript" /></div><Typography color="text.secondary" sx={{ mb: 1.5 }}>Use an API key with the <code>calls:read</code> scope. The transcript may be unavailable while a call is still running or if it could not be saved.</Typography><CopyableCode value={transcriptExample} minRows={3} /><Typography variant="subtitle2" sx={{ mt: 2, mb: 1 }}>Example response</Typography><pre className="docs-response">{`{
  "session_id": "SESSION_ID",
  "status": "completed",
  "transcript_available": true,
  "transcript_characters": 112,
  "transcript_persistence_failed": false,
  "transcript": "Agent: Hello, Avery.\\nCustomer: Yes, I confirm the appointment."
}`}</pre></section>
      <section className="docs-section" id="docs-webhooks">
        <Typography variant="h5">Fallback failure webhooks</Typography>
        <Typography color="text.secondary" sx={{ mb: 1.5 }}>A fallback webhook notifies your service when Chiefvoice cannot complete a call or its post-call processing. It lets your system update the record, alert a team, or schedule a follow-up without relying on a successful AI result.</Typography>
        <Typography variant="subtitle1" sx={{ fontWeight: 750, mt: 2 }}>When it is sent</Typography>
        <ul><li><code>call.failed</code>: the call fails, for example because the provider media pipeline fails.</li><li><code>call.post_call.failed</code>: post-call processing cannot continue, such as when no transcript is available or transcript persistence failed.</li></ul>
        <Typography variant="subtitle1" sx={{ fontWeight: 750, mt: 2 }}>Choose the destination</Typography>
        <Typography color="text.secondary">Configure the API’s failure fallback URL alongside its main Call outcome webhook URL. A post-call template may override the failure URL and add a static custom payload for calls using that template. Delivery chooses the template URL first, then the API failure fallback URL, then the main webhook URL. If no webhook is configured, no event is delivered.</Typography>
        <Typography variant="subtitle1" sx={{ fontWeight: 750, mt: 2, mb: 1 }}>Example event</Typography>
        <DocCodeBlock title="call.post_call.failed" value={`{
  "event": "call.post_call.failed",
  "event_id": "call.post_call.failed:SESSION_ID",
  "call": {
    "session_id": "SESSION_ID",
    "provider": "vobiz",
    "channel": "phone",
    "direction": "outbound",
    "started_at": "2026-11-18T10:00:00+00:00",
    "ended_at": "2026-11-18T10:01:00+00:00"
  },
  "failure": {
    "stage": "post_call",
    "code": "transcript_unavailable",
    "retryable": false,
    "attempts": null
  },
  "custom_payload": {
    "attendance_status": "unable_to_verify"
  }
}`} />
        <Typography variant="subtitle1" sx={{ fontWeight: 750, mt: 2 }}>Verify and process it safely</Typography>
        <Typography color="text.secondary" sx={{ mb: 1.5 }}>The webhook signing secret is returned when webhook settings are configured. Store it securely. Verify the signature against the exact raw request body before parsing JSON. Use <code>event_id</code> as an idempotency key because a delivery may be repeated. Return any 2xx status after accepting the event; network failures and non-2xx responses are retried with increasing delays until delivery stops retrying.</Typography>
        <DocCodeBlock title="Python signature check and idempotent handling" value={webhookVerifyExample} />
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>Headers include <code>X-Chiefvoice-Event</code>, <code>X-Chiefvoice-Event-Id</code>, <code>X-Chiefvoice-Timestamp</code>, and <code>X-Chiefvoice-Signature</code>. The signature is HMAC-SHA256 over <code>timestamp.event_id.raw_body</code> and is prefixed with <code>sha256=</code>. Failure codes are stable identifiers; transcript text is not included.</Typography>
      </section>
      <section className="docs-section" id="docs-recordings"><Typography variant="h5">Download a recording</Typography><Typography color="text.secondary" sx={{ mb: 1.5 }}>Recording must be enabled for both the API and the call.</Typography><CopyableCode value={recordingExample} minRows={3} /></section>
    </main>
  </div>
}
function CopyIconButton({ value, label, disabled = false }: { value: string; label: string; disabled?: boolean }) {
  const [copied, setCopied] = useState(false)
  const [failed, setFailed] = useState(false)
  const copy = () => {
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(true)
      setFailed(false)
    }).catch(() => {
      setCopied(false)
      setFailed(true)
    })
  }
  return <Tooltip title={failed ? 'Copy failed' : copied ? 'Copied' : label}>
    <span><IconButton size="small" aria-label={label} disabled={disabled} color={failed ? 'error' : 'default'} onClick={copy}>{copied ? <CheckCircleRoundedIcon fontSize="small" /> : <ContentCopyRoundedIcon fontSize="small" />}</IconButton></span>
  </Tooltip>
}

function CopyableCode({ value, minRows = 2 }: { value: string; minRows?: number }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    void navigator.clipboard.writeText(value).then(() => setCopied(true)).catch(() => setCopied(false))
  }
  return <div className="docs-code"><pre style={{ minHeight: `${Math.min(minRows, 10) * 1.45}em` }}><code>{value}</code></pre><Tooltip title={copied ? 'Copied' : 'Copy'}><IconButton aria-label={copied ? 'Copied' : 'Copy code'} onClick={copy} className="docs-code-copy">{copied ? <CheckCircleRoundedIcon /> : <ContentCopyRoundedIcon />}</IconButton></Tooltip></div>
}
function DocCodeBlock({ title, value }: { title: string; value: string }) {
  return <Stack gap={0.75}><Typography variant="subtitle2" sx={{ fontWeight: 700 }}>{title}</Typography><CopyableCode value={value} minRows={value.split('\n').length} /></Stack>
}
function StatusChip({ enabled, label }: { enabled: boolean; label?: string }) { return <Chip size="small" label={label || (enabled ? 'Enabled' : 'Disabled')} color={enabled ? 'success' : 'default'} variant={enabled ? 'filled' : 'outlined'} sx={{ fontSize: 10, height: 22 }} /> }

