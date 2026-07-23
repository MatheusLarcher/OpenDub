import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:5501";
const SESSION_KEY = "dublar-video:active-job";
const initialStatus = { job: "idle", dub: "idle", video: "idle", subtitles: "idle" };
const enter = { hidden: { opacity: 0, y: 16 }, visible: { opacity: 1, y: 0 } };

function loadSavedSession() {
  try {
    return JSON.parse(window.localStorage.getItem(SESSION_KEY) || "{}");
  } catch {
    return {};
  }
}

function describeError(error, fallback) {
  try {
    const payload = JSON.parse(error.message);
    return payload.detail || fallback;
  } catch {
    return error.message || fallback;
  }
}

function formatTimestamp(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

export default function App() {
  const savedSession = loadSavedSession();
  const [youtubeUrl, setYoutubeUrl] = useState("");
  const [uploadFile, setUploadFile] = useState(null);
  const [jobId, setJobId] = useState(savedSession.jobId || "");
  const [sourceType, setSourceType] = useState(savedSession.sourceType || "");
  const [status, setStatus] = useState(initialStatus);
  const [errorMessage, setErrorMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [preserveOriginalVoice, setPreserveOriginalVoice] = useState(Boolean(savedSession.preserveOriginalVoice));
  const [subtitleSegments, setSubtitleSegments] = useState([]);
  const [dubFakeProgress, setDubFakeProgress] = useState(0);
  const [videoFakeProgress, setVideoFakeProgress] = useState(0);
  const [dubbedVideoPath, setDubbedVideoPath] = useState(null);
  const [videoOpened, setVideoOpened] = useState(false);
  const fileInput = useRef(null);
  const actionInFlight = useRef(false);

  useEffect(() => {
    if (!jobId) {
      window.localStorage.removeItem(SESSION_KEY);
      return;
    }
    window.localStorage.setItem(SESSION_KEY, JSON.stringify({ jobId, sourceType, preserveOriginalVoice }));
  }, [jobId, sourceType, preserveOriginalVoice]);

  useEffect(() => {
    if (!window.app?.onVideoDownloadComplete) return;
    window.app.onVideoDownloadComplete((filePath) => setDubbedVideoPath(filePath));
  }, []);

  useEffect(() => {
    if (!jobId || status.subtitles !== "done" || subtitleSegments.length) return;
    let cancelled = false;
    fetch(`${API_BASE}/export/transcription/${jobId}`)
      .then((response) => (response.ok ? response.json() : []))
      .then((segments) => { if (!cancelled) setSubtitleSegments(segments); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [jobId, status.subtitles, subtitleSegments.length]);

  useEffect(() => {
    if (!jobId) return undefined;
    let cancelled = false;
    const refreshJob = async () => {
      try {
        const response = await fetch(`${API_BASE}/jobs/${jobId}/status`);
        if (response.status === 404) {
          if (!cancelled) { window.localStorage.removeItem(SESSION_KEY); setJobId(""); }
          return;
        }
        if (!response.ok) throw new Error(await response.text());
        const data = await response.json();
        if (cancelled) return;
        setStatus((current) => ({
          ...current,
          job: data.media_ready ? "done" : current.job,
          dub: data.dub_ready ? "done" : data.processing_dub ? "loading" : current.dub,
          video: data.video_ready ? "done" : current.video,
          subtitles: data.subtitles_ready ? "done" : current.subtitles
        }));
        if (data.source_type) setSourceType(data.source_type);
        // O polling so pode LIGAR o loading (detectar dublagem em andamento apos um reload).
        // Quem desliga e sempre o fluxo local (dubVideo/generateSubtitles no finally) -- o
        // backend so marca "processing_dub" durante a traducao, nao durante gerar o video final,
        // entao deixar o poll desligar aqui apagava o loading antes do processo acabar de verdade.
        if (data.processing_dub) setBusy(true);
      } catch {
        // erro de rede transitorio: mantem o job atual e tenta de novo no proximo poll
      }
    };
    refreshJob();
    const pollId = window.setInterval(refreshJob, 2000);
    return () => { cancelled = true; window.clearInterval(pollId); };
  }, [jobId]);

  // Progresso "de verdade nunca soubemos quantos blocos faltam", então a barra avança sozinha
  // com o tempo, desacelerando conforme se aproxima do teto da etapa (nunca chega nele) --
  // ao terminar de verdade, o estado muda e a barra salta pro valor real da proxima etapa.
  // Isso nunca trava (sempre andando) e nunca regride (so' depende do tempo decorrido).
  useEffect(() => {
    if (status.dub !== "loading") { setDubFakeProgress(0); return undefined; }
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      const elapsedS = (Date.now() - startedAt) / 1000;
      setDubFakeProgress(1 - Math.exp(-elapsedS / 45));
    }, 400);
    return () => window.clearInterval(timer);
  }, [status.dub]);

  useEffect(() => {
    if (status.video !== "loading") { setVideoFakeProgress(0); return undefined; }
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      const elapsedS = (Date.now() - startedAt) / 1000;
      setVideoFakeProgress(1 - Math.exp(-elapsedS / 20));
    }, 400);
    return () => window.clearInterval(timer);
  }, [status.video]);

  const downloads = useMemo(() => jobId ? ({
    subtitles: `${API_BASE}/export/subtitles/${jobId}`,
    transcriptTxt: `${API_BASE}/export/transcript-txt/${jobId}`,
    video: `${API_BASE}/export/video/${jobId}`,
    original: `${API_BASE}/export/original/${jobId}`
  }) : null, [jobId]);

  const resetJob = () => {
    setJobId("");
    setSourceType("");
    setStatus(initialStatus);
    setErrorMessage("");
    setDubbedVideoPath(null);
    setVideoOpened(false);
  };

  const openDubbedVideo = async () => {
    if (!dubbedVideoPath || !window.app?.openPath) return;
    const failure = await window.app.openPath(dubbedVideoPath);
    if (failure) {
      setErrorMessage(`Não foi possível abrir o vídeo: ${failure}`);
      return;
    }
    setVideoOpened(true);
  };

  const chooseFile = (file) => {
    if (!file) return;
    if (!file.type.startsWith("video/")) {
      setErrorMessage("Escolha um arquivo de vídeo.");
      return;
    }
    setUploadFile(file);
    setYoutubeUrl("");
    setErrorMessage("");
  };

  const addVideo = async () => {
    if (busy || actionInFlight.current) return;
    if (!uploadFile && !youtubeUrl.trim()) {
      setErrorMessage("Cole um link do YouTube ou escolha um vídeo.");
      return;
    }
    actionInFlight.current = true;
    resetJob();
    setBusy(true);
    setStatus((current) => ({ ...current, job: "loading" }));
    try {
      let response;
      if (uploadFile) {
        const formData = new FormData();
        formData.append("file", uploadFile);
        response = await fetch(`${API_BASE}/process/upload`, { method: "POST", body: formData });
      } else {
        response = await fetch(`${API_BASE}/process/youtube`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: youtubeUrl.trim() })
        });
      }
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      setJobId(data.job_id);
      setSourceType(data.source_type || (uploadFile ? "upload" : "youtube"));
      setStatus((current) => ({ ...current, job: "done" }));
    } catch (error) {
      setErrorMessage(describeError(error, "Não foi possível preparar o vídeo."));
      setStatus((current) => ({ ...current, job: "error" }));
    } finally {
      actionInFlight.current = false;
      setBusy(false);
    }
  };

  const dubVideo = async () => {
    if (!jobId || busy || actionInFlight.current) return;
    actionInFlight.current = true;
    setBusy(true);
    setErrorMessage("");
    try {
      if (status.dub !== "done") {
        setStatus((current) => ({ ...current, dub: "loading" }));
        const response = await fetch(`${API_BASE}/dub`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ job_id: jobId, model_input: "deepfilter_original", preserve_original_voice: preserveOriginalVoice })
        });
        if (!response.ok) throw new Error(await response.text());
        await response.json();
        setStatus((current) => ({ ...current, dub: "done" }));
      }
      setStatus((current) => ({ ...current, video: "loading" }));
      const videoResponse = await fetch(`${API_BASE}/generate-video`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: jobId })
      });
      if (!videoResponse.ok) throw new Error(await videoResponse.text());
      setStatus((current) => ({ ...current, video: "done" }));
    } catch (error) {
      setErrorMessage(describeError(error, "Não foi possível dublar o vídeo."));
      setStatus((current) => ({ ...current, dub: "error", video: "error" }));
    } finally {
      actionInFlight.current = false;
      setBusy(false);
    }
  };

  const generateSubtitles = async () => {
    if (!jobId || busy || actionInFlight.current) return;
    actionInFlight.current = true;
    setBusy(true);
    setErrorMessage("");
    setStatus((current) => ({ ...current, subtitles: "loading" }));
    try {
      const response = await fetch(`${API_BASE}/subtitles/generate`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: jobId, confirm: true })
      });
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      setSubtitleSegments(data.segments || []);
      setStatus((current) => ({ ...current, subtitles: "done" }));
    } catch (error) {
      setErrorMessage(describeError(error, "Não foi possível gerar a legenda."));
      setStatus((current) => ({ ...current, subtitles: "error" }));
    } finally {
      actionInFlight.current = false;
      setBusy(false);
    }
  };

  const videoReady = status.video === "done";
  const label = busy ? (status.job === "loading" ? "Preparando vídeo…" : status.video === "loading" ? "Finalizando vídeo…" : "Traduzindo e dublando…") : "Dublar meu vídeo";
  const progress = videoReady ? 100
    : status.video === "loading" ? 74 + Math.round(23 * videoFakeProgress)
    : status.dub === "done" ? 74
    : status.dub === "loading" ? 25 + Math.round(45 * dubFakeProgress)
    : status.job === "done" ? 25
    : status.job === "loading" ? 10
    : 0;
  const steps = [
    { name: "Adicionar", done: status.job === "done", active: status.job === "loading" },
    { name: "Preparar", done: status.job === "done", active: status.job === "done" && status.dub === "idle" },
    { name: "Dublar", done: status.dub === "done", active: status.dub === "loading" },
    { name: "Finalizar", done: videoReady, active: status.video === "loading" }
  ];

  return (
    <main className="app-shell">
      <section className="studio">
        <header className="brand"><span className="brand-mark">O</span><span>OpenDub</span></header>
        <motion.div className="intro" initial="hidden" animate="visible" variants={enter} transition={{ duration: .45 }}>
          <p className="kicker">INGLÊS → PORTUGUÊS</p>
          <h1>Seu vídeo em português,<br /><em>em poucos cliques.</em></h1>
          <p>Adicione um vídeo. Nós cuidamos da dublagem e deixamos os arquivos prontos para baixar.</p>
        </motion.div>

        <motion.section className="progress-tracker" initial="hidden" animate="visible" variants={enter} transition={{ duration: .45, delay: .08 }} aria-label="Etapas do processo">
          <div className="progress-line"><motion.i animate={{ width: `${progress}%` }} transition={busy ? { ease: "linear", duration: .4 } : { type: "spring", stiffness: 75, damping: 18 }} /></div>
          <div className="progress-steps">{steps.map((step, index) => <div key={step.name} className={`progress-step ${step.done ? "is-done" : ""} ${step.active ? "is-active" : ""}`}><span>{step.done ? "✓" : step.active && busy ? <motion.i className="tiny-spinner" animate={{ rotate: 360 }} transition={{ repeat: Infinity, duration: .8, ease: "linear" }} /> : index + 1}</span><small>{step.name}</small></div>)}</div>
        </motion.section>

        <AnimatePresence mode="wait">{!jobId && <motion.section className="source-card" key="source" initial="hidden" animate="visible" exit="hidden" variants={enter} transition={{ duration: .3 }}>
          <div className="source-tabs"><span>1</span><strong>Adicione seu vídeo</strong></div>
          <label className="youtube-field">
            <span>Link do YouTube</span>
            <input value={youtubeUrl} onChange={(event) => { setYoutubeUrl(event.target.value); setUploadFile(null); }} placeholder="Cole o link aqui" disabled={busy} />
          </label>
          <div className="or"><span>ou</span></div>
          <button
            type="button"
            className={`dropzone ${dragging ? "is-dragging" : ""}`}
            onClick={() => fileInput.current?.click()}
            onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => { event.preventDefault(); setDragging(false); chooseFile(event.dataTransfer.files[0]); }}
            disabled={busy}
          >
            <span className="upload-icon">↑</span>
            <strong>{uploadFile ? uploadFile.name : "Arraste um vídeo aqui"}</strong>
            <small>{uploadFile ? "Arquivo pronto para enviar" : "ou clique para escolher no computador"}</small>
          </button>
          <input ref={fileInput} className="visually-hidden" type="file" accept="video/*" onChange={(event) => chooseFile(event.target.files?.[0])} />
          <button className="primary-button" onClick={addVideo} disabled={busy || (!uploadFile && !youtubeUrl.trim())}>{busy ? <><motion.i className="button-spinner" animate={{ rotate: 360 }} transition={{ repeat: Infinity, duration: .8, ease: "linear" }} /> Preparando vídeo…</> : <>Continuar <span>→</span></>}</button>
        </motion.section>}</AnimatePresence>

        <AnimatePresence>{jobId && <motion.section className="workflow-card" key="workflow" initial="hidden" animate="visible" variants={enter} transition={{ duration: .3 }}>
          <div className="step-row"><span className="done-dot">✓</span><div><strong>Vídeo adicionado</strong><small>Ele continua disponível mesmo se você recarregar a página.</small></div><button className={videoOpened ? "text-button is-cta cta-pulse" : "text-button"} onClick={resetJob} disabled={busy}>{videoOpened ? "Dublar outro vídeo" : "Trocar"}</button></div>
          {sourceType === "youtube" && status.job === "done" && <motion.a className="original-download" href={downloads.original} initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }}>
            <span className="download-mini-icon">↓</span><span><strong>Baixar vídeo original</strong><small>O vídeo do YouTube já está pronto para baixar.</small></span><b>Baixar</b>
          </motion.a>}
          <label className="voice-switch">
            <input type="checkbox" checked={preserveOriginalVoice} disabled={busy || status.dub === "done"} onChange={(event) => setPreserveOriginalVoice(event.target.checked)} />
            <span className="switch-track" />
            <span><strong>Manter entonação original</strong><small>Usa a voz do vídeo como referência. Pode demorar mais.</small></span>
          </label>
          {!videoReady && <button className="primary-button" onClick={dubVideo} disabled={busy || status.job !== "done"}>{busy ? <><motion.i className="button-spinner" animate={{ rotate: 360 }} transition={{ repeat: Infinity, duration: .8, ease: "linear" }} /> {label}</> : <>{label}<span>→</span></>}</button>}
          {busy && <motion.div className="processing" initial={{ opacity: 0 }} animate={{ opacity: 1 }}><i /><span>{status.dub === "loading" ? "Traduzindo, limpando e recriando a voz. Isso pode levar alguns minutos." : "Não feche esta página. Seu progresso será recuperado ao voltar."}</span></motion.div>}
        </motion.section>}</AnimatePresence>

        <AnimatePresence>{errorMessage && <motion.div className="error-message" initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}><strong>Algo não deu certo.</strong><span>{errorMessage}</span></motion.div>}</AnimatePresence>

        <AnimatePresence>{videoReady && <motion.section className="result-card" initial={{ opacity: 0, scale: .98 }} animate={{ opacity: 1, scale: 1 }} transition={{ type: "spring", stiffness: 170, damping: 20 }}>
          <div className="result-head"><span className="success-mark">✓</span><div><p className="kicker">TUDO PRONTO</p><h2>Seu vídeo foi dublado.</h2></div></div>
          <div className="downloads">
            <a className={`download-primary ${!dubbedVideoPath ? "cta-pulse" : ""}`} href={downloads.video}>Baixar vídeo dublado <span>↓</span></a>
            {dubbedVideoPath && (
              <motion.button
                type="button"
                className="open-video-button"
                onClick={openDubbedVideo}
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
              >
                Abrir vídeo <span>▶</span>
              </motion.button>
            )}
            {status.subtitles === "done" ? (
              <>
                <a href={downloads.subtitles}>Legenda .SRT <span>↓</span></a>
                <a href={downloads.transcriptTxt}>Transcrição .TXT <span>↓</span></a>
              </>
            ) : (
              <button onClick={generateSubtitles} disabled={busy}>{status.subtitles === "loading" ? <><motion.i className="button-spinner dark" animate={{ rotate: 360 }} transition={{ repeat: Infinity, duration: .8, ease: "linear" }} /> Gerando legenda…</> : <>Gerar legenda <span>→</span></>}</button>
            )}
            <a href={downloads.original}>Vídeo original <span>↓</span></a>
          </div>
          {status.subtitles === "done" && subtitleSegments.length > 0 && (
            <motion.div className="transcript" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
              <p className="kicker">TRANSCRIÇÃO</p>
              <div className="transcript-list">
                {subtitleSegments.map((segment, index) => (
                  <div key={`${segment.start}-${index}`} className="transcript-line">
                    <span>{formatTimestamp(segment.start)}</span>
                    <p>{segment.text}</p>
                  </div>
                ))}
              </div>
            </motion.div>
          )}
        </motion.section>}</AnimatePresence>
        <footer className="app-footer">Feito por <a href="https://larchertech.com" target="_blank" rel="noreferrer">LarcherTech AI</a></footer>
      </section>
      <aside className="visual-panel"><div className="orb orb-one" /><div className="orb orb-two" /><div className="wave-card"><span>traduzindo vozes</span><div className="wave">{Array.from({ length: 28 }, (_, i) => <i key={i} style={{ "--h": `${20 + ((i * 37) % 70)}%` }} />)}</div><small>sem perder o ritmo do vídeo</small></div><p>Som original.<br />Nova língua.</p></aside>
    </main>
  );
}
