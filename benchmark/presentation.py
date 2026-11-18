"""Public processing status is independent of internal queue and quality flags."""

PROCESSING_LABELS = {"processed": "Обработан", "unprocessed": "Не обработан", "running": "Обрабатывается"}


def processing_state(has_card, queue_state=None):
    if queue_state == "running":
        return "running"
    return "processed" if has_card else "unprocessed"


def processing_label(has_card, queue_state=None):
    return PROCESSING_LABELS[processing_state(has_card, queue_state)]
