from datetime import datetime, timedelta


def resolve_date_range(period: str) -> dict[str, str]:
    """
    Resolves a relative date period string into a start_date and end_date.
    Returns a dictionary with 'start_date' and 'end_date' in 'YYYY-MM-DD' format.
    If period is not recognized, defaults to last 30 days.
    """
    today = datetime.now().date()
    start_date = today
    end_date = today

    period = period.lower().strip().replace(" ", "_")

    if period == "today":
        start_date = today
        end_date = today

    elif period == "yesterday":
        start_date = today - timedelta(days=1)
        end_date = today - timedelta(days=1)

    elif period == "this_week":
        # Assuming week starts on Monday
        start_date = today - timedelta(days=today.weekday())
        end_date = today

    elif period == "last_week":
        # Last complete week (Mon-Sun)
        end_date = today - timedelta(days=today.weekday() + 1)
        start_date = end_date - timedelta(days=6)

    elif period == "this_month":
        start_date = today.replace(day=1)
        end_date = today

    elif period == "last_month":
        last_month_end = today.replace(day=1) - timedelta(days=1)
        start_date = last_month_end.replace(day=1)
        end_date = last_month_end

    elif period == "this_quarter":
        quarter_start_month = 3 * ((today.month - 1) // 3) + 1
        start_date = today.replace(month=quarter_start_month, day=1)
        end_date = today

    elif period == "last_quarter":
        current_quarter_start = today.replace(
            day=1, month=3 * ((today.month - 1) // 3) + 1
        )
        end_date = current_quarter_start - timedelta(days=1)
        start_date = end_date.replace(
            day=1, month=end_date.month - 2
        )  # Start of quarter is 2 months back from end month

    elif period == "this_year":
        start_date = today.replace(month=1, day=1)
        end_date = today

    elif period == "last_year":
        end_date = today.replace(month=1, day=1) - timedelta(days=1)
        start_date = end_date.replace(month=1, day=1)

    elif period == "last_30_days":
        start_date = today - timedelta(days=30)
        end_date = today

    elif period == "last_90_days":
        start_date = today - timedelta(days=90)
        end_date = today

    elif period == "last_7_days":
        start_date = today - timedelta(days=7)
        end_date = today

    else:
        # Default or fallback
        pass

    return {
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d"),
    }
