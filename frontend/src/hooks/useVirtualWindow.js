import { useEffect, useRef, useState } from "react";

/**
 * Fixed-row-height windowing for large tables — zero dependencies.
 *
 * Returns a scroll-container ref plus the visible [start,end) slice and
 * spacer heights. Render two full-width spacer rows around your visible
 * rows so column alignment stays native-table perfect.
 */
export function useVirtualWindow({ itemCount, rowHeight = 33, overscan = 10 }) {
  const ref = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportH, setViewportH] = useState(640);

  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    setViewportH(el.clientHeight);
    if (typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(() => setViewportH(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const start = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const visibleCount = Math.ceil(viewportH / rowHeight) + overscan * 2;
  const end = Math.min(itemCount, start + visibleCount);

  return {
    ref,
    onScroll: () => setScrollTop(ref.current?.scrollTop || 0),
    start,
    end,
    topPad: start * rowHeight,
    bottomPad: Math.max(0, (itemCount - end) * rowHeight),
    totalHeight: itemCount * rowHeight,
    viewportH,
  };
}

export default useVirtualWindow;
