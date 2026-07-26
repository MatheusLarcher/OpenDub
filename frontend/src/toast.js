// Sistema global de notificacoes (toasts) — mesmo padrao usado no CRM.
// Uso em qualquer arquivo:
//   import { toast } from "./toast"
//   toast.success("Vídeo dublado!")
//   toast.error("Não foi possível dublar.")
//   toast.warning("A legenda ficou incompleta.")
//   toast.info("Retomando o vídeo de antes.")

let _listeners = [];
let _id = 0;

function _emit(tipo, mensagem, duracao) {
  const item = { id: ++_id, tipo, mensagem, duracao };
  _listeners.forEach((fn) => fn(item));
}

export const toast = {
  success: (mensagem, duracao = 4500) => _emit("success", mensagem, duracao),
  error: (mensagem, duracao = 6500) => _emit("error", mensagem, duracao),
  warning: (mensagem, duracao = 5500) => _emit("warning", mensagem, duracao),
  info: (mensagem, duracao = 4500) => _emit("info", mensagem, duracao)
};

export function subscribeToasts(fn) {
  _listeners.push(fn);
  return () => { _listeners = _listeners.filter((l) => l !== fn); };
}
