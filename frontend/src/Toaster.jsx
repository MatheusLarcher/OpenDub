import { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { subscribeToasts } from "./toast";

// Mesma linguagem visual do Toaster do CRM (borda esquerda colorida por tipo, icone,
// botao de fechar e barra que esvazia), mas com os icones em SVG inline: o OpenDub nao
// usa lucide-react e nao vale puxar uma dependencia so para quatro icones.
const ESTILOS = {
  success: { cor: "#3f7654", trilha: "#7ba98c", caminho: "M4 12.5l5 5L20 6.5" },
  error: { cor: "#a8483b", trilha: "#d08b81", caminho: "M12 7v7M12 17.2v.3" },
  warning: { cor: "#a37312", trilha: "#d6b26a", caminho: "M12 8v6M12 17.2v.3" },
  info: { cor: "#3c6584", trilha: "#8fb0c6", caminho: "M12 10.5v7M12 6.8v.3" }
};

const ANEL = "M12 2.5a9.5 9.5 0 100 19 9.5 9.5 0 000-19z";

function ToastItem({ item, onClose }) {
  const estilo = ESTILOS[item.tipo] || ESTILOS.info;
  const fecharRef = useRef(onClose);
  fecharRef.current = onClose;

  useEffect(() => {
    const timer = window.setTimeout(() => fecharRef.current(item.id), item.duracao);
    return () => window.clearTimeout(timer);
  }, [item.id, item.duracao]);

  const fechar = useCallback(() => onClose(item.id), [item.id, onClose]);

  return (
    <motion.div
      className="toast"
      role="status"
      style={{ "--toast-cor": estilo.cor }}
      initial={{ opacity: 0, x: 28, scale: .97 }}
      animate={{ opacity: 1, x: 0, scale: 1 }}
      exit={{ opacity: 0, x: 28, scale: .97 }}
      transition={{ duration: .24, ease: "easeOut" }}
      layout
    >
      <div className="toast-body">
        <svg className="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          {item.tipo !== "success" && <path d={ANEL} />}
          <path d={estilo.caminho} />
        </svg>
        <p>{item.mensagem}</p>
        <button type="button" onClick={fechar} aria-label="Fechar notificação">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      </div>
      <motion.i
        className="toast-bar"
        style={{ background: estilo.trilha }}
        initial={{ width: "100%" }}
        animate={{ width: "0%" }}
        transition={{ duration: item.duracao / 1000, ease: "linear" }}
      />
    </motion.div>
  );
}

export default function Toaster() {
  const [toasts, setToasts] = useState([]);

  // Guarda no maximo 5 na tela: alem disso o canto vira uma parede de avisos.
  useEffect(() => subscribeToasts((item) => setToasts((prev) => [...prev.slice(-4), item])), []);

  const remover = useCallback((id) => setToasts((prev) => prev.filter((t) => t.id !== id)), []);

  return (
    <div className="toaster" aria-live="polite">
      <AnimatePresence initial={false}>
        {toasts.map((t) => <ToastItem key={t.id} item={t} onClose={remover} />)}
      </AnimatePresence>
    </div>
  );
}
