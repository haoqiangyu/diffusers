from dataclasses import dataclass
from typing import Tuple, Union

import torch
import numpy as np
import torch.nn.functional as F

from ..configuration_utils import ConfigMixin, register_to_config
from ..utils import BaseOutput
from .scheduling_utils import SchedulerMixin

@dataclass
class VQDiffusionSchedulerOutput(BaseOutput):
    x_t_min_1: torch.LongTensor

def alpha_schedules(num_diffusion_timesteps, att_1 = 0.99999, att_T = 0.000009):
    att = np.arange(0, num_diffusion_timesteps)/(num_diffusion_timesteps-1)*(att_T - att_1) + att_1
    att = np.concatenate(([1], att))
    at = att[1:]/att[:-1]
    att = np.concatenate((att[1:], [1]))
    return at, att


def gamma_schedules(time_step, ctt_1 = 0.000009, ctt_T = 0.99999):
    ctt = np.arange(0, time_step)/(time_step-1)*(ctt_T - ctt_1) + ctt_1
    ctt = np.concatenate(([0], ctt))
    one_minus_ctt = 1 - ctt
    one_minus_ct = one_minus_ctt[1:] / one_minus_ctt[:-1]
    ct = 1-one_minus_ct
    ctt = np.concatenate((ctt[1:], [0]))
    return ct, ctt

class VQDiffusionScheduler(SchedulerMixin, ConfigMixin):
    """
    TODO

    For more details, see the original paper: https://arxiv.org/abs/2111.14822


    Args:
        TODO
    """
    @register_to_config
    def __init__(
        self,
        # number of classes, including the mask
        num_embed: int,
        num_train_timesteps: int = 100,
        min_logged_value: float = -70.0,
    ):
        self.num_embed = num_embed
        self.min_logged_value = min_logged_value

        # By convention, the index for the mask class is the last class index
        self.mask_class = self.num_embed - 1

        at, att = alpha_schedules(num_train_timesteps)
        ct, ctt = gamma_schedules(num_train_timesteps)

        num_non_mask_classes = self.num_embed - 1
        bt = (1-at-ct) / num_non_mask_classes
        btt = (1-att-ctt) / num_non_mask_classes

        at = torch.tensor(at.astype('float64'))
        bt = torch.tensor(bt.astype('float64'))
        ct = torch.tensor(ct.astype('float64'))
        log_at = torch.log(at)
        log_bt = torch.log(bt)
        log_ct = torch.log(ct)

        att = torch.tensor(att.astype('float64'))
        btt = torch.tensor(btt.astype('float64'))
        ctt = torch.tensor(ctt.astype('float64'))
        log_cumprod_at = torch.log(att)
        log_cumprod_bt = torch.log(btt)
        log_cumprod_ct = torch.log(ctt)

        log_1_min_ct = log_1_min_a(log_ct)
        log_1_min_cumprod_ct = log_1_min_a(log_cumprod_ct)

        self.log_at = log_at.float()
        self.log_bt = log_bt.float()
        self.log_ct = log_ct.float()
        self.log_cumprod_at = log_cumprod_at.float()
        self.log_cumprod_bt = log_cumprod_bt.float()
        self.log_cumprod_ct = log_cumprod_ct.float()
        self.log_1_min_ct = log_1_min_ct.float()
        self.log_1_min_cumprod_ct = log_1_min_cumprod_ct.float()

        # setable values
        self.num_inference_steps = None
        self.timesteps = torch.from_numpy(np.arange(0, num_train_timesteps)[::-1].copy())


    def set_timesteps(self, num_inference_steps: int, device: Union[str, torch.device] = None):
        """
        Sets the discrete timesteps used for the diffusion chain. Supporting function to be run before inference.

        Args:
            num_inference_steps (`int`):
                the number of diffusion steps used when generating samples with a pre-trained model.
        """
        self.num_inference_steps = num_inference_steps
        timesteps = np.arange(0, self.num_inference_steps)[::-1].copy()
        self.timesteps = torch.from_numpy(timesteps).to(device)


    def step(
        self, 
        log_p_x_0, 
        x_t, 
        t,
        return_dict: bool = True,
    ) -> Union[VQDiffusionSchedulerOutput, Tuple]:
        # add the zero vector to log_p_x_0
        bsz, _, num_latent_pixels = log_p_x_0.shape
        log_zero_vector = torch.full((bsz, 1, num_latent_pixels), self.min_logged_value, device=log_p_x_0.device)
        log_p_x_0 = torch.cat((log_p_x_0, log_zero_vector), dim=1)

        # convert x_t into log_probs
        log_x_t = index_to_log_onehot(x_t, self.num_embed)

        model_log_prob = self.q_posterior(log_p_x_0, log_x_t, t)
        out = self.log_sample_categorical(model_log_prob)

        x_t_min_1 = out

        if not return_dict:
            return (x_t_min_1,)

        return VQDiffusionSchedulerOutput(x_t_min_1=x_t_min_1)

    def q_posterior(self, log_x_start, log_x_t, t):            # p_theta(xt_1|xt) = sum(q(xt-1|xt,x0')*p(x0'))
        # notice that log_x_t is onehot
        # assert t.min().item() >= 0 and t.max().item() < self.num_timesteps
        _, _, content_seq_len = log_x_t

        batch_size = log_x_start.size()[0]
        onehot_x_t = log_onehot_to_index(log_x_t)
        mask = (onehot_x_t == self.num_embed-1).unsqueeze(1) 
        log_one_vector = torch.zeros(batch_size, 1, 1).type_as(log_x_t)
        log_zero_vector = torch.log(log_one_vector+1.0e-30).expand(-1, -1, content_seq_len)

        log_qt = self.q_pred(log_x_t, t)                                  # q(xt|x0)
        # log_qt = torch.cat((log_qt[:,:-1,:], log_zero_vector), dim=1)
        log_qt = log_qt[:,:-1,:]
        log_cumprod_ct = extract(self.log_cumprod_ct, t, log_x_start.shape)         # ct~
        ct_cumprod_vector = log_cumprod_ct.expand(-1, self.num_embed-1, -1)
        # ct_cumprod_vector = torch.cat((ct_cumprod_vector, log_one_vector), dim=1)
        log_qt = (~mask)*log_qt + mask*ct_cumprod_vector
        

        log_qt_one_timestep = self.q_pred_one_timestep(log_x_t, t)        # q(xt|xt_1)
        log_qt_one_timestep = torch.cat((log_qt_one_timestep[:,:-1,:], log_zero_vector), dim=1)
        log_ct = extract(self.log_ct, t, log_x_start.shape)         # ct
        ct_vector = log_ct.expand(-1, self.num_embed-1, -1)
        ct_vector = torch.cat((ct_vector, log_one_vector), dim=1)
        log_qt_one_timestep = (~mask)*log_qt_one_timestep + mask*ct_vector
        
        # log_x_start = torch.cat((log_x_start, log_zero_vector), dim=1)
        # q = log_x_start - log_qt
        q = log_x_start[:,:-1,:] - log_qt
        q = torch.cat((q, log_zero_vector), dim=1)
        q_log_sum_exp = torch.logsumexp(q, dim=1, keepdim=True)
        q = q - q_log_sum_exp
        log_EV_xtmin_given_xt_given_xstart = self.q_pred(q, t-1) + log_qt_one_timestep + q_log_sum_exp
        return torch.clamp(log_EV_xtmin_given_xt_given_xstart, -70, 0)

    def log_sample_categorical(self, logits):           # use gumbel to sample onehot vector from log probability
        uniform = torch.rand_like(logits)
        gumbel_noise = -torch.log(-torch.log(uniform + 1e-30) + 1e-30)
        noised = gumbel_noise + logits
        sample = noised.argmax(dim=1)
        log_sample = index_to_log_onehot(sample, self.num_embed)

        # num_masked = (sample == self.num_embed - 1).count_nonzero()
        # print()
        # print("***********")
        # print()
        # print(f"num masked {num_masked}")
        # print()
        # print("unnoised mask probabilities")
        # print(logits[0, -1, :].exp())
        # print()
        # print("noised mask probabilities")
        # print(noised[0, -1, :].exp())
        # print()
        # print("***********")
        # print()

        return log_sample

    def q_pred_one_timestep(self, log_x_t, t):         # q(xt|xt_1)
        log_at = extract(self.log_at, t, log_x_t.shape)             # at
        log_bt = extract(self.log_bt, t, log_x_t.shape)             # bt
        log_ct = extract(self.log_ct, t, log_x_t.shape)             # ct
        log_1_min_ct = extract(self.log_1_min_ct, t, log_x_t.shape)          # 1-ct

        log_probs = torch.cat(
            [
                log_add_exp(log_x_t[:,:-1,:]+log_at, log_bt),
                log_add_exp(log_x_t[:, -1:, :] + log_1_min_ct, log_ct)
            ],
            dim=1
        )

        return log_probs

    def q_pred(self, log_x_start, t):           # q(xt|x0)
        # log_x_start can be onehot or not
        t = (t + (self.num_timesteps + 1))%(self.num_timesteps + 1)
        log_cumprod_at = extract(self.log_cumprod_at, t, log_x_start.shape)         # at~
        log_cumprod_bt = extract(self.log_cumprod_bt, t, log_x_start.shape)         # bt~
        log_cumprod_ct = extract(self.log_cumprod_ct, t, log_x_start.shape)         # ct~
        log_1_min_cumprod_ct = extract(self.log_1_min_cumprod_ct, t, log_x_start.shape)       # 1-ct~
        

        log_probs = torch.cat(
            [
                log_add_exp(log_x_start[:,:-1,:]+log_cumprod_at, log_cumprod_bt),
                log_add_exp(log_x_start[:,-1:,:]+log_1_min_cumprod_ct, log_cumprod_ct)
            ],
            dim=1
        )

        return log_probs


def extract(a, t, x_shape):
    b, *_ = t.shape
    out = a.gather(-1, t)
    return out.reshape(b, *((1,) * (len(x_shape) - 1)))

def log_onehot_to_index(log_x):
    return log_x.argmax(1)

def log_add_exp(a, b):
    maximum = torch.max(a, b)
    return maximum + torch.log(torch.exp(a - maximum) + torch.exp(b - maximum))

def log_1_min_a(a):
    return torch.log(1 - a.exp() + 1e-40)

def index_to_log_onehot(x, num_classes):
    assert x.max().item() < num_classes, \
        f'Error: {x.max().item()} >= {num_classes}'
    x_onehot = F.one_hot(x, num_classes)
    permute_order = (0, -1) + tuple(range(1, len(x.size())))
    x_onehot = x_onehot.permute(permute_order)
    log_x = torch.log(x_onehot.float().clamp(min=1e-30))
    return log_x
