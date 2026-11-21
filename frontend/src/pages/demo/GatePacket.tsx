import { useLayoutEffect, useRef } from "react";
import type { Lane } from "./gateTimeline";

type Point = { x: number; y: number };
type PacketState = "moving" | "waiting" | "failed" | "suspended";

function translation({ x, y }: Point) {
  return `translate(${x}px, ${y}px)`;
}

function travelFrames(points: Point[]): Keyframe[] {
  let distance = 0;
  const frames = points.map(({ x, y }, i) => {
    if (i > 0) distance += Math.hypot(x - points[i - 1].x, y - points[i - 1].y);
    return { transform: translation({ x, y }), offset: distance };
  });
  return frames.map(frame => ({ ...frame, offset: distance ? frame.offset / distance : 1 }));
}

export function GatePacket({ lane, state, duration }: {
  lane?: Lane;
  state: PacketState;
  duration: number;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const previousDock = useRef<HTMLElement | null>(null);

  useLayoutEffect(() => {
    const packet = ref.current;
    const panel = packet?.parentElement;
    const dock = lane && panel?.querySelector<HTMLElement>(`[data-packet-lane="${lane}"]`);
    if (!packet || !panel || !dock) {
      previousDock.current = null;
      return;
    }

    const origin = () => {
      const { left, top } = panel.getBoundingClientRect();
      return { x: left + panel.clientLeft, y: top + panel.clientTop };
    };

    function pointFor(element: HTMLElement, from: Point): Point {
      const { left, top } = element.getBoundingClientRect();
      return { x: left - from.x, y: top - from.y };
    }

    const from = origin();
    const previous = previousDock.current;
    previousDock.current = dock;
    let destination = pointFor(dock, from);
    packet.style.transform = translation(destination);

    let motion: Animation | undefined;
    if (duration > 0 && previous && previous !== dock && previous.isConnected && packet.animate) {
      const start = pointFor(previous, from);
      const turn = dock.parentElement?.querySelector<HTMLElement>(".gate-lane-turn");
      const route = [start];
      if (previous.parentElement?.nextElementSibling === dock.parentElement && turn?.getClientRects().length) {
        const y = turn.getBoundingClientRect().bottom - from.y;
        route.push({ x: start.x, y }, { x: destination.x, y });
      }
      route.push(destination);
      motion = packet.animate(travelFrames(route), { duration, easing: "ease-in-out" });
    }

    // The grid can wrap while playback is paused or in progress.
    const observer = new ResizeObserver(() => {
      const next = pointFor(dock, origin());
      if (next.x === destination.x && next.y === destination.y) return;
      motion?.cancel();
      destination = next;
      packet.style.transform = translation(next);
    });
    observer.observe(panel);
    const stages = panel.querySelector(".gate-lanes");
    if (stages) observer.observe(stages);
    return () => {
      observer.disconnect();
      motion?.cancel();
    };
  }, [lane, duration]);

  return <span ref={ref} className="gate-packet" data-state={state} hidden={!lane} aria-hidden="true" />;
}
