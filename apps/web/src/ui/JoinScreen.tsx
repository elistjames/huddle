import { Huddle } from "./design";

/** Guest join (design/components/JoinForm): one name field, no account. */
export function JoinScreen({
  initialName,
  failed,
  onJoin,
}: {
  initialName: string;
  failed: boolean;
  onJoin: (name: string) => void;
}) {
  return (
    <div className="center">
      <div className="card">
        <Huddle.JoinForm initialName={initialName} onJoin={onJoin} />
        {failed ? (
          <p className="join-error" role="alert">
            Couldn’t join. Try again.
          </p>
        ) : null}
      </div>
    </div>
  );
}
