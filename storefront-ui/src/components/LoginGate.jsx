// Full-screen sign-in wall shown until the visitor authenticates with Google.
// The rest of the app (search, talk, watches) only renders once signed in.
export default function LoginGate({ onSignIn }) {
  return (
    <div className="login-gate">
      <div className="login-card">
        <span className="brand">
          <span className="brand-dot" />
          TALKPROD
        </span>
        <h1 className="login-title">Your AI shopping concierge</h1>
        <p className="login-sub">
          Sign in to search with the concierge, talk to real product opinions, and
          track prices for the things you want.
        </p>
        <button className="btn btn-solid login-btn" onClick={onSignIn}>
          <span className="nav-auth-g">G</span>
          Sign in with Google
        </button>
        <span className="login-fine">
          We use your Google email only to save your price watches.
        </span>
      </div>
    </div>
  )
}
