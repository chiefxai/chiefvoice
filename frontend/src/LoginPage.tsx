import HubRoundedIcon from '@mui/icons-material/HubRounded'
import LockOutlinedIcon from '@mui/icons-material/LockOutlined'
import { Alert, Button, CircularProgress, Paper, Typography } from '@mui/material'
import { oidcAuthority } from './auth'

type Props = {
  loading: boolean
  signingIn: boolean
  error: string
  onSignIn: () => void
}

export default function LoginPage({ loading, signingIn, error, onSignIn }: Props) {
  return (
    <main className="login-page">
      <div className="login-glow login-glow-one" />
      <div className="login-glow login-glow-two" />
      <Paper className="login-card" elevation={0}>
        <div className="login-brand-mark"><HubRoundedIcon /></div>
        <div className="login-eyebrow">CHIEFVOICE PLATFORM</div>
        <Typography variant="h4" className="login-title">Sign in to continue</Typography>
        <Typography className="login-description">
          Use your organization identity to manage phone numbers and customer API access.
        </Typography>
        {error && <Alert severity="error" sx={{ mb: 2.5, textAlign: 'left' }}>{error}</Alert>}
        <Button
          fullWidth
          size="large"
          variant="contained"
          disabled={loading || signingIn}
          onClick={onSignIn}
          startIcon={loading || signingIn ? <CircularProgress size={18} color="inherit" /> : <LockOutlinedIcon />}
          sx={{ py: 1.35, mt: 1 }}
        >
          {loading ? 'Checking sign-in…' : signingIn ? 'Redirecting…' : 'Sign in with OIDC'}
        </Button>
        <div className="login-provider">Identity provider<br /><code>{oidcAuthority}</code></div>
      </Paper>
      <div className="login-footer">Secure access for Chiefvoice administrators</div>
    </main>
  )
}
