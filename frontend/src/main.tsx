import React from 'react'
import ReactDOM from 'react-dom/client'
import { useMemo, useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { CssBaseline, ThemeProvider, createTheme, type PaletteMode } from '@mui/material'
import App from './App'
import './styles.css'
import './responsive.css'

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 15_000 } } })
function Root() {
  const [mode, setMode] = useState<PaletteMode>(() => localStorage.getItem('chiefvoice.theme') === 'dark' ? 'dark' : 'light')
  const theme = useMemo(() => createTheme({
    palette: mode === 'light' ? {
      mode, primary: { main: '#4159d8' }, secondary: { main: '#10a88a' }, background: { default: '#f5f7fb', paper: '#fff' }, text: { primary: '#18243a', secondary: '#758097' }, success: { main: '#16a582' }, warning: { main: '#e5a33e' }, error: { main: '#df6d66' }, divider: '#e9edf4',
    } : {
      mode, primary: { main: '#9aa9ff' }, secondary: { main: '#59d4b5' }, background: { default: '#0c1422', paper: '#141f31' }, text: { primary: '#e7edf8', secondary: '#a0aec2' }, success: { main: '#59d4b5' }, warning: { main: '#f0bb68' }, error: { main: '#f28a82' }, divider: '#2a374c',
    },
    typography: { fontFamily: 'DM Sans, sans-serif', h1: { fontFamily: 'Manrope, sans-serif', fontWeight: 800 }, h2: { fontFamily: 'Manrope, sans-serif', fontWeight: 800 }, h3: { fontFamily: 'Manrope, sans-serif', fontWeight: 800 }, h4: { fontFamily: 'Manrope, sans-serif', fontWeight: 800 }, h5: { fontFamily: 'Manrope, sans-serif', fontWeight: 700 }, h6: { fontFamily: 'Manrope, sans-serif', fontWeight: 700 }, button: { textTransform: 'none', fontWeight: 700 } },
    shape: { borderRadius: 12 }, components: { MuiButton: { styleOverrides: { root: { borderRadius: 10, paddingInline: 16, boxShadow: 'none' } } }, MuiCard: { styleOverrides: { root: { border: `1px solid ${mode === 'light' ? '#e9edf4' : '#26364d'}`, borderRadius: 16, boxShadow: mode === 'light' ? '0 4px 18px rgba(29,43,74,.035)' : '0 5px 24px rgba(0,0,0,.16)' } } }, MuiOutlinedInput: { styleOverrides: { root: { borderRadius: 10 } } } },
  }), [mode])
  const toggleMode = () => setMode(current => {
    const next = current === 'light' ? 'dark' : 'light'
    localStorage.setItem('chiefvoice.theme', next)
    return next
  })
  return <ThemeProvider theme={theme}><CssBaseline /><App mode={mode} onToggleTheme={toggleMode} /></ThemeProvider>
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><QueryClientProvider client={queryClient}><Root /></QueryClientProvider></React.StrictMode>,
)
