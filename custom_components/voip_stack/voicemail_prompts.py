"""Short IVR phrases spoken by the globally selected Home Assistant TTS."""

_PHRASES = {
    "en": {
        "count": "You have {count} messages.", "empty": "You have no messages.",
        "message": "Message {number}, received from {caller}.",
        "choices": "Press 1 for the next message, 2 to delete, or 3 to repeat. Press 0 to select a message, then hash. Hash ends the call.",
        "deleted": "Message deleted.", "missing": "That message is no longer available.",
        "invalid": "Invalid selection.", "last": "There are no more messages.",
        "mailbox": "Enter the mailbox extension, followed by hash.",
        "pin": "Enter the mailbox PIN, followed by hash.", "denied": "Mailbox access denied.",
        "leave": "Please leave your message after the tone.",
    },
    "it": {
        "count": "Hai {count} messaggi.", "empty": "Non hai messaggi.",
        "message": "Messaggio {number}, ricevuto da {caller}.",
        "choices": "Premi 1 per il successivo, 2 per eliminare, 3 per riascoltare. Premi 0 per scegliere un messaggio e termina con cancelletto. Cancelletto chiude la chiamata.",
        "deleted": "Messaggio eliminato.", "missing": "Il messaggio non è più disponibile.",
        "invalid": "Selezione non valida.", "last": "Non ci sono altri messaggi.",
        "mailbox": "Digita l'interno della casella, seguito da cancelletto.",
        "pin": "Digita il PIN della casella, seguito da cancelletto.", "denied": "Accesso alla casella negato.",
        "leave": "Lascia il tuo messaggio dopo il segnale acustico.",
    },
    "de": {
        "count": "Sie haben {count} Nachrichten.", "empty": "Sie haben keine Nachrichten.",
        "message": "Nachricht {number}, empfangen von {caller}.",
        "choices": "Drücken Sie 1 für die nächste Nachricht, 2 zum Löschen oder 3 zum Wiederholen. Mit 0 wählen Sie eine Nachricht und bestätigen mit Raute. Raute beendet den Anruf.",
        "deleted": "Nachricht gelöscht.", "missing": "Diese Nachricht ist nicht mehr verfügbar.",
        "invalid": "Ungültige Auswahl.", "last": "Keine weiteren Nachrichten.",
        "mailbox": "Geben Sie die Nebenstelle ein und bestätigen Sie mit Raute.",
        "pin": "Geben Sie die PIN ein und bestätigen Sie mit Raute.", "denied": "Zugriff verweigert.",
        "leave": "Bitte hinterlassen Sie Ihre Nachricht nach dem Signalton.",
    },
    "pt": {
        "count": "Você tem {count} mensagens.", "empty": "Você não tem mensagens.",
        "message": "Mensagem {number}, recebida de {caller}.",
        "choices": "Pressione 1 para a próxima mensagem, 2 para excluir ou 3 para repetir. Pressione 0 para selecionar uma mensagem e termine com cardinal. Cardinal encerra a chamada.",
        "deleted": "Mensagem excluída.", "missing": "Esta mensagem não está mais disponível.",
        "invalid": "Seleção inválida.", "last": "Não há mais mensagens.",
        "mailbox": "Digite o ramal da caixa e termine com cardinal.",
        "pin": "Digite o PIN e termine com cardinal.", "denied": "Acesso negado.",
        "leave": "Deixe sua mensagem após o sinal.",
    },
}


def phrase(language, key, **values):
    language = language.replace("_", "-").split("-", 1)[0].lower()
    return _PHRASES[language][key].format(**values)
