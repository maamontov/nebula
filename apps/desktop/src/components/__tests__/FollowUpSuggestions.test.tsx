import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { FollowUpSuggestions } from '../FollowUpSuggestions';
import * as api from '../../services/api';
import canonicalFixture from '../../../../../tests/fixtures/canonical_followups_state.json';

vi.mock('../../services/api', () => ({
  getFollowUpsState: vi.fn(),
  generateFollowUps: vi.fn(),
  patchFollowUpSuggestion: vi.fn(),
  retryFollowUpRequest: vi.fn(),
}));

describe('FollowUpSuggestions Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders canonical backend fixture fields (question_text, purpose, quote) correctly', async () => {
    vi.mocked(api.getFollowUpsState).mockResolvedValue(canonicalFixture as any);

    render(
      <FollowUpSuggestions
        interviewId="int-test-1"
        questionId="q-backend-saga"
        questionTitle="Saga Pattern"
        segments={[]}
        onLocateSegment={vi.fn()}
      />
    );

    // Check that question_text is rendered
    await waitFor(() => {
      expect(
        screen.getByText('Как именно вы обеспечиваете идемпотентность компенсирующих транзакций?')
      ).toBeInTheDocument();
    });

    // Check that purpose is rendered
    expect(
      screen.getByText('Проверить понимание идемпотентности при сбоях сети')
    ).toBeInTheDocument();

    // Check that evidence quote is rendered
    expect(
      screen.getByText(/Мы использовали Saga для компенсаций/)
    ).toBeInTheDocument();
  });

  it('resets activeMode to probe when question changes (R8)', async () => {
    vi.mocked(api.getFollowUpsState).mockResolvedValue(canonicalFixture as any);

    const { rerender } = render(
      <FollowUpSuggestions
        interviewId="int-test-1"
        questionId="q-1"
        questionTitle="Question 1"
        segments={[]}
        onLocateSegment={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(api.getFollowUpsState).toHaveBeenCalledWith('int-test-1', 'q-1', 'probe');
    });

    // Click Guide button ('Мягко направить')
    const guideBtn = screen.getByText('Мягко направить');
    fireEvent.click(guideBtn);

    // Verify polling or state switched to guide
    await waitFor(() => {
      expect(api.getFollowUpsState).toHaveBeenCalledWith('int-test-1', 'q-1', 'guide');
    });

    // Change question to q-2
    rerender(
      <FollowUpSuggestions
        interviewId="int-test-1"
        questionId="q-2"
        questionTitle="Question 2"
        segments={[]}
        onLocateSegment={vi.fn()}
      />
    );

    // On fixed code, switching question MUST reset activeMode to 'probe'
    await waitFor(() => {
      expect(api.getFollowUpsState).toHaveBeenCalledWith('int-test-1', 'q-2', 'probe');
    });
  });
});
