import { useEffect, useRef, useState } from 'react'

// "Talk to your product": opening the drawer kicks off Reddit ingestion on the
// backend, polls until the knowledge base is ready, then lets the user chat. The
// answers come from Gemini grounded on retrieved Reddit opinions.

const SUGGESTED = [
  'How is the battery life?',
  'Any common problems or complaints?',
  'Is it worth buying in 2026?',
  'How is the camera in low light?',
]

export default function TalkDrawer({ product, onClose, onQuota }) {
  const open = !!product
  const productName = product?.name || ''
  // Identity for ingestion/retrieval: the concierge's canonical `model` (same across
  // colour/storage variants) so we don't re-scrape the same phone. Falls back to the
  // display name if an older result has no model. Display still uses productName.
  const productKey = product?.model || product?.name || ''

  const [phase, setPhase] = useState('idle')   // idle | preparing | ready | error
  const [statusMsg, setStatusMsg] = useState('')
  const [quota, setQuota] = useState(null)

  // Mirror quota updates up to the app so the global meter stays in sync.
  const reportQuota = (q) => {
    if (!q || q.remaining == null) return
    setQuota(q)
    onQuota?.(q)
  }
  const [messages, setMessages] = useState([])  // {role, content, sources?}
  const [input, setInput] = useState('')
  const [asking, setAsking] = useState(false)

  const pollRef = useRef(null)
  const bodyRef = useRef(null)

  // Kick off ingestion whenever a new product is opened.
  useEffect(() => {
    if (!open) return
    setPhase('preparing')
    setStatusMsg('Getting ready…')
    setMessages([])
    setInput('')

    let cancelled = false

    fetch('/talk/prepare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product: productKey }),
    }).catch(() => {})

    const poll = async () => {
      try {
        const res = await fetch(`/talk/status?product=${encodeURIComponent(productKey)}`)
        const data = await res.json()
        if (cancelled) return
        if (data.quota) reportQuota(data.quota)
        if (data.status === 'ready') {
          setPhase('ready')
          setStatusMsg(data.message || 'Ready.')
          clearInterval(pollRef.current)
        } else if (data.status === 'error') {
          setPhase('error')
          setStatusMsg(data.message || 'Something went wrong.')
          clearInterval(pollRef.current)
        } else {
          setStatusMsg(data.message || 'Working…')
        }
      } catch (_) { /* keep polling */ }
    }

    poll()
    pollRef.current = setInterval(poll, 1500)
    return () => {
      cancelled = true
      clearInterval(pollRef.current)
    }
  }, [open, productKey])

  // Keep the conversation scrolled to the latest message.
  useEffect(() => {
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, asking])

  const ask = async (question) => {
    const q = (question ?? input).trim()
    if (!q || phase !== 'ready' || asking) return
    setInput('')
    const history = messages.map((m) => ({ role: m.role, content: m.content }))
    setMessages((prev) => [...prev, { role: 'user', content: q }])
    setAsking(true)
    try {
      const res = await fetch('/talk/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ product: productKey, question: q, history }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || 'Request failed')
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: data.answer, sources: data.sources || [] },
      ])
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: `⚠ ${err.message}`, error: true },
      ])
    } finally {
      setAsking(false)
    }
  }

  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      ask()
    }
  }

  return (
    <>
      <div className={`drawer-overlay ${open ? 'open' : ''}`} onClick={onClose} />
      <aside className={`talk-drawer ${open ? 'open' : ''}`} aria-hidden={!open}>
        <div className="talk-head">
          <div>
            <span className="eyebrow">Talk to your product</span>
            <h3>{productName}</h3>
          </div>
          <button className="drawer-close" onClick={onClose} aria-label="Close">×</button>
        </div>

        {quota && quota.remaining != null && (
          <div className={`quota-bar ${quota.remaining <= 10 ? 'low' : ''}`}>
            <span className="quota-dot" />
            Reddit API: {quota.remaining}/{quota.limit} requests left this month
          </div>
        )}

        <div className="talk-body" ref={bodyRef}>
          {phase === 'preparing' && (
            <div className="talk-status">
              <div className="spinner" />
              <p>{statusMsg}</p>
              <span className="talk-status-sub">
                Reading what real owners say on Reddit — this takes a moment.
              </span>
            </div>
          )}

          {phase === 'error' && (
            <div className="talk-status err">
              <p>{statusMsg}</p>
            </div>
          )}

          {phase === 'ready' && messages.length === 0 && (
            <div className="talk-intro">
              <p>{statusMsg}</p>
              <p className="talk-intro-sub">Ask anything about this product:</p>
              <div className="talk-suggestions">
                {SUGGESTED.map((s) => (
                  <button key={s} className="chip" onClick={() => ask(s)}>{s}</button>
                ))}
              </div>
            </div>
          )}

          {messages.map((m, i) => (
            <div key={i} className={`bubble ${m.role} ${m.error ? 'err' : ''}`}>
              <div className="bubble-text">{m.content}</div>
              {m.sources && m.sources.length > 0 && (
                <div className="bubble-sources">
                  {m.sources.slice(0, 4).map((s, j) => (
                    <a key={j} href={s} target="_blank" rel="noreferrer">source {j + 1}</a>
                  ))}
                </div>
              )}
            </div>
          ))}

          {asking && (
            <div className="bubble assistant">
              <div className="typing"><span /><span /><span /></div>
            </div>
          )}
        </div>

        <div className="talk-input">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={phase === 'ready' ? 'Ask about this product…' : 'Preparing…'}
            disabled={phase !== 'ready'}
            rows={1}
          />
          <button
            className="btn btn-solid"
            onClick={() => ask()}
            disabled={phase !== 'ready' || asking || !input.trim()}
          >
            Send
          </button>
        </div>
      </aside>
    </>
  )
}
