import type * as React from 'react';

export type IconName = 'bell' | 'bell-off' | 'check' | 'check-check' | 'chevron-down' | 'circle-alert' | 'clock' | 'copy' | 'download' | 'ellipsis' | 'log-out' | 'message-square' | 'paperclip' | 'pause' | 'pin' | 'play' | 'plus' | 'reply' | 'rotate-ccw' | 'search' | 'send' | 'settings' | 'smile' | 'trash-2' | 'user-plus' | 'users' | 'wifi-off' | 'x';
export interface IconProps { name: IconName; size?: number; strokeWidth?: number; label?: string; className?: string }
export declare function Icon(props: IconProps): React.ReactElement | null;

/** Pass `icon` with children for icon + label; `icon` without children makes an icon-only button, which then needs `label`. */
export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> { variant?: 'primary' | 'quiet' | 'ghost'; size?: 'md' | 'sm'; icon?: IconName; label?: string; block?: boolean }
export declare function Button(props: ButtonProps): React.ReactElement;

/** `idle` is for a paused, resting or away AI. v1 has no presence. */
export interface AvatarProps { name: string; kind?: 'person' | 'agent'; size?: 'md' | 'sm'; idle?: boolean; className?: string }
export declare function Avatar(props: AvatarProps): React.ReactElement;

/** sending = clock; stored = one tick; delivered = two; agent-read = two plus an amber third. */
export type ReceiptStatus = 'sending' | 'stored' | 'delivered' | 'agent-read';
export interface ReceiptProps { status: ReceiptStatus; agentName?: string; className?: string }
export declare function Receipt(props: ReceiptProps): React.ReactElement | null;

export type ErrorCode = 'RATE_LIMITED' | 'MESSAGE_TOO_LONG' | 'NOT_MEMBER' | 'SESSION_CLOSED';
export interface MessageBubbleProps { from?: 'other' | 'agent' | 'self'; tail?: boolean; status?: ReceiptStatus | 'failed'; errorCode?: ErrorCode; onRetry?: () => void; agentName?: string; className?: string; children?: React.ReactNode }
export declare function MessageBubble(props: MessageBubbleProps): React.ReactElement;

export type Message = string | { text: React.ReactNode; status?: ReceiptStatus | 'failed'; errorCode?: ErrorCode; onRetry?: () => void };
export interface MessageGroupProps { author?: string; kind?: 'other' | 'agent' | 'self'; time?: string; messages: Message[]; agentName?: string; className?: string }
export declare function MessageGroup(props: MessageGroupProps): React.ReactElement;

export type AgentState = 'following' | 'writing' | 'paused' | 'resting' | 'away';
/** `listening` is accepted as an alias for `following`. */
export interface AgentIndicatorProps { name?: string; state?: AgentState | 'listening'; backAt?: Date | number | string; className?: string }
export declare function AgentIndicator(props: AgentIndicatorProps): React.ReactElement;

export interface SystemNoteProps { time?: string; className?: string; children?: React.ReactNode }
export declare function SystemNote(props: SystemNoteProps): React.ReactElement;

export interface ComposerProps { state?: 'ready' | 'connecting' | 'reconnecting' | 'closed'; placeholder?: string; disabled?: boolean; maxLength?: number; closedText?: string; onSend?: (text: string) => void; onExport?: () => void; className?: string }
export declare function Composer(props: ComposerProps): React.ReactElement;

export interface EmptyStateProps { className?: string; children?: React.ReactNode }
export declare function EmptyState(props: EmptyStateProps): React.ReactElement;

export interface ThreadSkeletonProps { className?: string }
export declare function ThreadSkeleton(props: ThreadSkeletonProps): React.ReactElement;

export interface Member { name: string; you?: boolean }
export interface BudgetInfo { remaining: number; capacity: number; refillsAt?: Date | number | string }
export interface AgentInfo { name: string; state: AgentState; backAt?: Date | number | string; budget?: BudgetInfo }

export interface ThreadHeaderProps { title: string; connection?: 'connected' | 'connecting' | 'reconnecting' | 'closed'; members?: Member[]; agent?: AgentInfo; onOpenDetails?: () => void; className?: string }
export declare function ThreadHeader(props: ThreadHeaderProps): React.ReactElement;

export interface BudgetMeterProps { remaining: number; capacity: number; refillsAt?: Date | number | string; resting?: boolean; name?: string; className?: string }
export declare function BudgetMeter(props: BudgetMeterProps): React.ReactElement;

export interface SessionDetailsProps { members?: Member[]; agent?: AgentInfo; onPause?: () => void; onResume?: () => void; onRename?: (name: string) => void; onClose?: () => void; className?: string }
export declare function SessionDetails(props: SessionDetailsProps): React.ReactElement;

export interface JoinFormProps { initialName?: string; onJoin?: (name: string) => void; className?: string }
export declare function JoinForm(props: JoinFormProps): React.ReactElement;

/** Phase 3: multiple sessions. Not used in v1. */
export interface SessionRowProps extends React.ButtonHTMLAttributes<HTMLButtonElement> { title: string; preview?: string; time?: string; unread?: number; active?: boolean; hasAgent?: boolean }
export declare function SessionRow(props: SessionRowProps): React.ReactElement;

/** The Huddle mark (and wordmark). `tone="soft"` is amber on amber-soft (default); `"solid"` is the app-icon inversion. */
export interface LogoProps { variant?: 'lockup' | 'mark'; tone?: 'soft' | 'solid'; size?: number; className?: string }
export declare function Logo(props: LogoProps): React.ReactElement;

declare global { interface Window { Huddle: { Button: typeof Button; Avatar: typeof Avatar; Receipt: typeof Receipt; MessageBubble: typeof MessageBubble; MessageGroup: typeof MessageGroup; AgentIndicator: typeof AgentIndicator; SystemNote: typeof SystemNote; Composer: typeof Composer; EmptyState: typeof EmptyState; ThreadSkeleton: typeof ThreadSkeleton; ThreadHeader: typeof ThreadHeader; BudgetMeter: typeof BudgetMeter; SessionDetails: typeof SessionDetails; JoinForm: typeof JoinForm; SessionRow: typeof SessionRow; Icon: typeof Icon; Logo: typeof Logo } } }
