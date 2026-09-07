import { useEffect, useState } from 'react'

export default function Navbar({ quota, user, onSignIn, onSignOut, onOpenWatches }) {
  const [scrolled, setScrolled] = useState(false)

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 24)
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  const hasQuota = quota && quota.remaining != null
  const low = hasQuota && quota.remaining <= 10

  return (
    <nav className={`nav ${scrolled ? 'scrolled' : ''}`}>
      <div className="wrap nav-inner">
        <a href="#top" className="brand">
          <span className="brand-dot" />
          TALKPROD
        </a>

        <div className="nav-right">
          {hasQuota && (
            <div
              className={`nav-quota ${low ? 'low' : ''}`}
              title="Reddit API requests remaining this month (free tier)"
            >
              <span className="nav-quota-dot" />
              <span className="nav-quota-label">Reddit API</span>
              <span className="nav-quota-count mono">{quota.remaining}/{quota.limit}</span>
            </div>
          )}

          {user ? (
            <>
              <button
                className="nav-watch"
                onClick={onOpenWatches}
                title="Products you're watching"
              >
                <span className="nav-watch-star">★</span>
                <span className="nav-watch-label">Watching</span>
              </button>
              <button
                className="nav-auth"
                onClick={onSignOut}
                title={`Signed in as ${user.email} — click to sign out`}
              >
                <span className="nav-auth-label">{user.name || user.email}</span>
                <span className="nav-auth-action">Sign out</span>
              </button>
            </>
          ) : (
            <button className="nav-auth nav-auth-in" onClick={onSignIn}>
              <span className="nav-auth-g">G</span>
              Sign in
            </button>
          )}
        </div>
      </div>
    </nav>
  )
}
