"""Read back what instruments recorded, off an `InMemoryMetricReader`, in a shape a test can compare whole."""

from __future__ import annotations

from opentelemetry.sdk.metrics import export as metrics_export

Labels = frozenset[tuple[str, str]]


def labels(**values: str) -> Labels:
    """A series' label set, as `collected` keys it."""
    return frozenset(values.items())


def collected(reader: metrics_export.InMemoryMetricReader) -> dict[str, dict[Labels, int | float]]:
    """Every series the reader collects, by metric name then label set, each with its latest value.

    Collects on call, so a counter reads cumulative and a gauge reads its last `set`.
    """
    data = reader.get_metrics_data()
    series: dict[str, dict[Labels, int | float]] = {}
    if data is None:
        return series
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                points = series.setdefault(metric.name, {})
                for point in metric.data.data_points:
                    if not isinstance(point, metrics_export.NumberDataPoint):
                        raise TypeError(f'{metric.name}: {type(point).__name__} is not a number point')
                    if point.attributes is None:
                        raise TypeError(f'{metric.name}: a point without attributes')
                    points[frozenset((str(k), str(v)) for k, v in point.attributes.items())] = point.value
    return series
