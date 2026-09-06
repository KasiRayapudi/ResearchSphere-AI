import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { EmptyState, ErrorState, FullPageLoader, Skeleton } from './States';
import { Badge } from './Badge';
import { Button } from './Button';

describe('Skeleton', () => {
  it('is hidden from assistive technology', () => {
    const { container } = render(<Skeleton className="h-4" />);
    expect(container.firstChild).toHaveAttribute('aria-hidden', 'true');
  });
});

describe('EmptyState', () => {
  it('renders the title and description', () => {
    render(<EmptyState title="No documents yet" description="Upload a file to begin." />);
    expect(screen.getByText('No documents yet')).toBeInTheDocument();
    expect(screen.getByText('Upload a file to begin.')).toBeInTheDocument();
  });

  it('invokes the action callback', async () => {
    const onClick = vi.fn();
    render(<EmptyState title="Empty" action={{ label: 'Upload', onClick }} />);
    await userEvent.click(screen.getByRole('button', { name: 'Upload' }));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it('renders no button when no action is supplied', () => {
    render(<EmptyState title="Empty" />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});

describe('ErrorState', () => {
  it('is announced as an alert', () => {
    render(<ErrorState message="Backend unreachable" />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('Backend unreachable')).toBeInTheDocument();
  });

  it('offers a retry when a handler is provided', async () => {
    const onRetry = vi.fn();
    render(<ErrorState onRetry={onRetry} />);
    await userEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});

describe('FullPageLoader', () => {
  it('shows the supplied label', () => {
    render(<FullPageLoader label="Restoring your session" />);
    expect(screen.getByText('Restoring your session')).toBeInTheDocument();
  });
});

describe('Button', () => {
  it('renders its children', () => {
    render(<Button>Save</Button>);
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
  });

  it('is disabled while loading', () => {
    render(<Button isLoading>Save</Button>);
    expect(screen.getByRole('button')).toBeDisabled();
  });

  it('does not fire onClick when disabled', async () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Save
      </Button>
    );
    await userEvent.click(screen.getByRole('button'));
    expect(onClick).not.toHaveBeenCalled();
  });
});

describe('Badge', () => {
  it('renders its label', () => {
    render(<Badge variant="success">indexed</Badge>);
    expect(screen.getByText('indexed')).toBeInTheDocument();
  });
});
