import { useCallback, useEffect, useRef, useState } from "react";
import { subscribeToasts } from "./toast";

// Mesma linguagem visual e a mesma mecanica do Toaster do CRM: a saida e controlada por
// estado (`saindo`) e o item so sai da lista depois da animacao, entao o no e realmente
// desmontado. Tentei fazer com AnimatePresence do framer-motion e o no ficava para sempre
// no DOM com opacity 0 -- invisivel, mas acumulando e capturando clique.
//
// Icones em SVG inline: o OpenDub nao usa lucide-react e nao vale puxar uma dependencia
// so para quatro icones.
const ESTILOS = {
  success: { cor: "#3f7654", trilha: "#7ba98c", caminho: "M4 12.5l5 5L20 6.5", anel: false },
  error: { cor: "#a8483b", trilha: "#d08b81", caminho: "M12 7v7M12 17.2v.3", anel: true },
  warning: { cor: "#a37312", trilha: "#d6b26a", caminho: "M12 8v6M12 17.2v.3", anel: true },
  info: { cor: "#3c6584", trilha: "#8fb0c6", caminho: "M12 10.5v7M12 6.8v.3", anel: true }
};

const ANEL = "M12 2.5a9.5 9.5 0 100 19 9.5 9.5 0 000-19z";
const SAIDA_MS = 220;

function ToastItem({ item, onClose }) {
  const estilo = ESTILOS[item.tipo] || ESTILOS.info;
  const [saindo, setSaindo] = useState(false);
  const saidaTimer = useRef(null);
  const fecharRef = useRef(null);

  const fechar = useCallback(() => {
    setSaindo(true);
    window.clearTimeout(saidaTimer.current);
    saidaTimer.current = window.setTimeout(() => onClose(item.id), SAIDA_MS);
  }, [item.id, onClose]);
  fecharRef.current = fechar;

  useEffect(() => {
    const timer = window.setTimeout(() => fecharRef.current(), item.duracao);
    return () => { window.clearTimeout(timer); window.clearTimeout(saidaTimer.current); };
  }, [item.id, item.duracao]);

  return (
    <div
      className={`toast ${saindo ? "is-leaving" : ""}`}
      role="status"
      style={{ "--toast-cor": estilo.cor }}
    >
      <div className="toast-body">
        <svg className="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          {estilo.anel && <path d={ANEL} />}
          <path d={estilo.caminho} />
        </svg>
        <p>{item.mensagem}</p>
        <button type="button" onClick={fechar} aria-label="Fechar notificação">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      </div>
      <i
        className="toast-bar"
        style={{ background: estilo.trilha, animation: `toast-bar ${item.duracao}ms linear forwards` }}
      />
    </div>
  );
}

export default function Toaster() {
  const [toasts, setToasts] = useState([]);

  // Guarda no maximo 5 na tela: alem disso o canto vira uma parede de avisos.
  useEffect(() => subscribeToasts((item) => setToasts((prev) => [...prev.slice(-4), item])), []);

  const remover = useCallback((id) => setToasts((prev) => prev.filter((t) => t.id !== id)), []);

  if (toasts.length === 0) return null;

  return (
    <div className="toaster" aria-live="polite">
      {toasts.map((t) => <ToastItem key={t.id} item={t} onClose={remover} />)}
    </div>
  );
}
