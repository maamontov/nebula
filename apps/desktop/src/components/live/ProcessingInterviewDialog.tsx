import React, { useEffect } from 'react';
import { StopSessionDialog } from './StopSessionDialog';
import { useInterviewFinalizer } from './useInterviewFinalizer';

interface ProcessingInterviewDialogProps {
  interviewId: string;
  onFinished: (interviewId: string) => void;
  onClose: () => void;
}

/** Finishes processing of an interview whose recording was stopped earlier (status `processing`). */
export const ProcessingInterviewDialog: React.FC<ProcessingInterviewDialogProps> = ({ interviewId, onFinished, onClose }) => {
  const finalizer = useInterviewFinalizer({ interviewId, alreadyStopped: true, onFinished });
  const { run, cancel } = finalizer;

  useEffect(() => {
    void run();
    return cancel;
  }, [run, cancel]);

  const leave = () => {
    cancel();
    onClose();
  };

  return (
    <StopSessionDialog
      phase="progress"
      resumed
      elapsedMs={0}
      askedCount={0}
      totalQuestions={0}
      evaluatedCount={0}
      steps={finalizer.steps}
      error={finalizer.error}
      captureStopped
      canAcceptIncomplete={finalizer.canAcceptIncomplete}
      waitingLong={finalizer.waitingLong}
      onCancel={leave}
      onConfirm={() => void run()}
      onRetry={() => void run()}
      onAcceptIncomplete={() => void run({ acceptIncomplete: true })}
      onLeave={leave}
    />
  );
};
