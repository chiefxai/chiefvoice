import { UserManager, WebStorageStateStore, type User } from 'oidc-client-ts'

const authority = import.meta.env.VITE_OIDC_AUTHORITY || 'http://localhost:9000'
const clientId = import.meta.env.VITE_OIDC_CLIENT_ID || 'chiefvoice-console'
const redirectUri = import.meta.env.VITE_OIDC_REDIRECT_URI || `${window.location.origin}/`

const userManager = new UserManager({
  authority,
  client_id: clientId,
  redirect_uri: redirectUri,
  response_type: 'code',
  scope: import.meta.env.VITE_OIDC_SCOPE || 'openid profile email platform:customers:read platform:customers:write',
  userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
  automaticSilentRenew: false,
  monitorSession: false,
  loadUserInfo: false,
})

export const login = () => userManager.signinRedirect()
let callbackPromise: Promise<User> | undefined
export const finishLogin = () => {
  callbackPromise ??= userManager.signinRedirectCallback()
  return callbackPromise
}
export const currentUser = () => userManager.getUser()
export const logout = () => userManager.removeUser()

export function clearLoginCallbackFromUrl() {
  window.history.replaceState({}, document.title, window.location.pathname)
}

export const oidcAuthority = authority
