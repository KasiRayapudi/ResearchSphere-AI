import React from 'react';
import { Crown, Eye, Pencil, Shield } from 'lucide-react';
import { Badge } from '../common/Badge';
import { WorkspaceRole } from '../../types';

/**
 * Role badges use the existing Badge variants rather than new colours, so a
 * workspace role reads the same way as every other status in the product.
 * Icon plus text: colour alone would leave the roles indistinguishable to
 * anyone who cannot separate amber from emerald.
 */
const STYLES: Record<
  WorkspaceRole,
  { variant: 'brand' | 'success' | 'warning' | 'neutral'; icon: React.ReactNode; label: string }
> = {
  owner: { variant: 'brand', icon: <Crown className="h-3 w-3" />, label: 'Owner' },
  admin: { variant: 'warning', icon: <Shield className="h-3 w-3" />, label: 'Admin' },
  editor: { variant: 'success', icon: <Pencil className="h-3 w-3" />, label: 'Editor' },
  viewer: { variant: 'neutral', icon: <Eye className="h-3 w-3" />, label: 'Viewer' },
};

export const RoleBadge: React.FC<{ role: WorkspaceRole; size?: 'sm' | 'md' }> = ({
  role,
  size = 'sm',
}) => {
  const style = STYLES[role] ?? STYLES.viewer;
  return (
    <Badge variant={style.variant} size={size} icon={style.icon}>
      {style.label}
    </Badge>
  );
};
