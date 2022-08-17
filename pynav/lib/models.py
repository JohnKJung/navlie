from pynav.types import (
    ProcessModel,
    MeasurementModel,
    StampedValue,
)
from pynav.lib.states import (
    CompositeState,
    MatrixLieGroupState,
    SE3State,
    VectorState,
)
from pylie import SO2, SO3
import numpy as np
from typing import List
from scipy.linalg import block_diag


class SingleIntegrator(ProcessModel):
    """
    The single-integrator process model is a process model of the form

        x_dot = u .
    """

    def __init__(self, Q: np.ndarray):

        if Q.shape[0] != Q.shape[1]:
            raise ValueError("Q must be an n x n matrix.")

        self._Q = Q
        self.dim = Q.shape[0]

    def evaluate(self, x: VectorState, u: StampedValue, dt: float) -> np.ndarray:
        x.value = x.value + dt * u.value
        return x

    def jacobian(self, x, u, dt) -> np.ndarray:
        return np.identity(self.dim)

    def covariance(self, x, u, dt) -> np.ndarray:
        return dt**2 * self._Q


class BodyFrameVelocity(ProcessModel):
    """
    The body-frame velocity process model assumes that the input contains
    both translational and angular velocity measurements, both relative to
    a local reference frame, but resolved in the robot body frame.

    This is commonly the process model associated with SE(n).
    """

    def __init__(self, Q: np.ndarray):
        self._Q = Q

    def evaluate(
        self, x: MatrixLieGroupState, u: StampedValue, dt: float
    ) -> MatrixLieGroupState:
        x.value = x.value @ x.group.Exp(u.value * dt)
        return x

    def jacobian(
        self, x: MatrixLieGroupState, u: StampedValue, dt: float
    ) -> np.ndarray:
        if x.direction == "right":
            return x.group.adjoint(x.group.Exp(-u.value * dt))
        else:
            raise NotImplementedError("TODO: left jacobian not yet implemented.")

    def covariance(
        self, x: MatrixLieGroupState, u: StampedValue, dt: float
    ) -> np.ndarray:
        if x.direction == "right":
            L = dt * x.group.left_jacobian(-u.value * dt)
            return L @ self._Q @ L.T
        else:
            raise NotImplementedError("TODO: left covariance not yet implemented.")


class RelativeBodyFrameVelocity(ProcessModel):
    def __init__(self, Q1: np.ndarray, Q2: np.ndarray):
        self._Q1 = Q1
        self._Q2 = Q2

    def evaluate(
        self, x: MatrixLieGroupState, u: StampedValue, dt: float
    ) -> MatrixLieGroupState:
        u = u.value.reshape((2, round(u.value.size / 2)))
        x.value = x.group.Exp(-u[0] * dt) @ x.value @ x.group.Exp(u[1] * dt)
        return x

    def jacobian(
        self, x: MatrixLieGroupState, u: StampedValue, dt: float
    ) -> np.ndarray:
        u = u.value.reshape((2, round(u.value.size / 2)))
        if x.direction == "right":
            return x.group.adjoint(x.group.Exp(-u[1] * dt))
        else:
            raise NotImplementedError("TODO: left jacobian not yet implemented.")

    def covariance(
        self, x: MatrixLieGroupState, u: StampedValue, dt: float
    ) -> np.ndarray:
        u = u.value.reshape((2, round(u.value.size / 2)))
        u1 = u[0]
        u2 = u[1]
        if x.direction == "right":
            L1 = (
                dt
                * x.group.adjoint(x.value @ x.group.Exp(u2 * dt))
                @ x.group.left_jacobian(dt * u1)
            )
            L2 = dt * x.group.left_jacobian(-dt * u2)
            return L1 @ self._Q1 @ L1.T + L2 @ self._Q2 @ L2.T
        else:
            raise NotImplementedError("TODO: left covariance not yet implemented.")


class CompositeProcessModel(ProcessModel):
    """
    Should this be called a StackedProcessModel?
    """

    def __init__(self, model_list: List[ProcessModel]):
        self._model_list = model_list

    def evaluate(self, x: CompositeState, u: StampedValue, dt: float) -> CompositeState:
        for i, x_sub in enumerate(x.value):
            u_sub = StampedValue(u.value[i], u.stamp)
            x.value[i] = self._model_list[i].evaluate(x_sub, u_sub, dt)

        return x

    def jacobian(self, x: CompositeState, u: StampedValue, dt: float) -> np.ndarray:
        jac = []
        for i, x_sub in enumerate(x.value):
            u_sub = StampedValue(u.value[i], u.stamp)
            jac.append(self._model_list[i].jacobian(x_sub, u_sub, dt))

        return block_diag(*jac)

    def covariance(self, x: CompositeState, u: StampedValue, dt: float) -> np.ndarray:
        cov = []
        for i, x_sub in enumerate(x.value):
            u_sub = StampedValue(u.value[i], u.stamp)
            cov.append(self._model_list[i].covariance(x_sub, u_sub, dt))

        return block_diag(*cov)


class CompositeMeasurementModel(MeasurementModel):
    """
    Wrapper for a standard measurement model that assigns the model to a specific
    substate (referenced by `state_id`) inside a CompositeState.
    """

    def __init__(self, model: MeasurementModel, state_id):
        self._model = model
        self._state_id = state_id

    def evaluate(self, x: CompositeState) -> np.ndarray:
        return self._model.evaluate(x.get_state_by_id(self._state_id))

    def jacobian(self, x: CompositeState) -> np.ndarray:
        x_sub = x.get_state_by_id(self._state_id)
        jac_sub = self._model.jacobian(x_sub)
        jac = np.zeros((jac_sub.shape[0], x.dof))
        slc = x.get_slice_by_id(self._state_id)
        jac[:, slc] = jac_sub
        return jac

    def covariance(self, x: CompositeState) -> np.ndarray:
        x_sub = x.get_state_by_id(self._state_id)
        return self._model.covariance(x_sub)


class RangePointToAnchor(MeasurementModel):
    """
    Range measurement from a point state to an anchor (which is also another
    point).
    """

    def __init__(self, anchor_position: List[float], R: float):
        self._r_cw_a = np.array(anchor_position).flatten()
        self._R = np.array(R)

    def evaluate(self, x: VectorState) -> np.ndarray:
        r_zw_a = x.value.flatten()
        y = np.linalg.norm(self._r_cw_a - r_zw_a)
        return y

    def jacobian(self, x: VectorState) -> np.ndarray:
        r_zw_a = x.value.flatten()
        r_zc_a: np.ndarray = r_zw_a - self._r_cw_a
        y = np.linalg.norm(r_zc_a)
        return r_zc_a.reshape((1, -1)) / y

    def covariance(self, x: VectorState) -> np.ndarray:
        return self._R


class RangePoseToAnchor(MeasurementModel):
    """
    Range measurement from a pose state to an anchor.
    """

    def __init__(
        self,
        anchor_position: List[float],
        tag_body_position: List[float],
        R: float,
    ):
        self._r_cw_a = np.array(anchor_position).flatten()
        self._R = R
        self._r_tz_b = np.array(tag_body_position).flatten()

    def evaluate(self, x: MatrixLieGroupState) -> np.ndarray:
        r_zw_a = x.position
        C_ab = x.attitude

        r_tw_a = C_ab @ self._r_tz_b.reshape((-1, 1)) + r_zw_a.reshape((-1, 1))
        r_tc_a: np.ndarray = r_tw_a - self._r_cw_a.reshape((-1, 1))
        return np.linalg.norm(r_tc_a)

    def jacobian(self, x: MatrixLieGroupState) -> np.ndarray:
        if x.direction == "right":
            r_zw_a = x.position
            C_ab = x.attitude
            if C_ab.shape == (2, 2):
                att_group = SO2
            elif C_ab.shape == (3, 3):
                att_group = SO3

            r_tw_a = C_ab @ self._r_tz_b.reshape((-1, 1)) + r_zw_a.reshape((-1, 1))
            r_tc_a: np.ndarray = r_tw_a - self._r_cw_a.reshape((-1, 1))
            rho = r_tc_a / np.linalg.norm(r_tc_a)
            jac_attitude = rho.T @ C_ab @ att_group.odot(self._r_tz_b)
            jac_position = rho.T @ C_ab
            jac = x.jacobian_from_blocks(
                attitude=jac_attitude,
                position=jac_position,
            )
            return jac
        else:
            raise NotImplementedError("Left jacobian not implemented.")

    def covariance(self, x: MatrixLieGroupState) -> np.ndarray:
        return self._R


class RangePoseToPose(MeasurementModel):
    """
    Range model given two absolute poses of rigid bodies, each containing a tag.
    """

    def __init__(self, tag_body_position1, tag_body_position2, state_id1, state_id2, R):
        self._r_t1_1 = np.array(tag_body_position1).flatten()
        self._r_t2_2 = np.array(tag_body_position2).flatten()
        self._id1 = state_id1
        self._id2 = state_id2
        self._R = R

    def evaluate(self, x: CompositeState) -> np.ndarray:
        x1: MatrixLieGroupState = x.get_state_by_id(self._id1)
        x2: MatrixLieGroupState = x.get_state_by_id(self._id2)
        r_1w_a = x1.position.reshape((-1, 1))
        C_a1 = x1.attitude
        r_2w_a = x2.position.reshape((-1, 1))
        C_a2 = x2.attitude
        r_t1_1 = self._r_t1_1.reshape((-1, 1))
        r_t2_2 = self._r_t2_2.reshape((-1, 1))
        r_t1t2_a: np.ndarray = (C_a1 @ r_t1_1 + r_1w_a) - (C_a2 @ r_t2_2 + r_2w_a)
        return np.linalg.norm(r_t1t2_a.flatten())

    def jacobian(self, x: CompositeState) -> np.ndarray:
        x1: MatrixLieGroupState = x.get_state_by_id(self._id1)
        x2: MatrixLieGroupState = x.get_state_by_id(self._id2)
        r_1w_a = x1.position.reshape((-1, 1))
        C_a1 = x1.attitude
        r_2w_a = x2.position.reshape((-1, 1))
        C_a2 = x2.attitude
        r_t1_1 = self._r_t1_1.reshape((-1, 1))
        r_t2_2 = self._r_t2_2.reshape((-1, 1))
        r_t1t2_a: np.ndarray = (C_a1 @ r_t1_1 + r_1w_a) - (C_a2 @ r_t2_2 + r_2w_a)

        if C_a1.shape == (2, 2):
            att_group = SO2
        elif C_a1.shape == (3, 3):
            att_group = SO3

        rho: np.ndarray = (r_t1t2_a / np.linalg.norm(r_t1t2_a.flatten())).reshape(
            (-1, 1)
        )
        jac1 = x1.jacobian_from_blocks(
            attitude=rho.T @ C_a1 @ att_group.odot(r_t1_1),
            position=rho.T @ C_a1,
        )
        jac2 = x2.jacobian_from_blocks(
            attitude=-rho.T @ C_a2 @ att_group.odot(r_t2_2),
            position=-rho.T @ C_a2,
        )

        slc1 = x.get_slice_by_id(self._id1)
        slc2 = x.get_slice_by_id(self._id2)

        jac = np.zeros((1, x.dof))
        jac[:, slc1] = jac1
        jac[:, slc2] = jac2
        return jac

    def covariance(self, x: CompositeState) -> np.ndarray:
        return self._R


class RangeRelativePose(CompositeMeasurementModel):
    """
    Range model given a pose of another body relative to current pose.
    """

    def __init__(self, tag_body_position, nb_tag_body_position, nb_state_id, R):
        model = RangePoseToAnchor(tag_body_position, nb_tag_body_position, R)
        super(RangeRelativePose, self).__init__(model, nb_state_id)


class GlobalPosition(MeasurementModel):
    """
    Global, world-frame, or "absolute" position measurement.
    """

    def __init__(self, R: np.ndarray):
        self.R = R

    def evaluate(self, x: MatrixLieGroupState):
        return x.position

    def jacobian(self, x: MatrixLieGroupState):
        C_ab = x.attitude
        if C_ab.shape == (2, 2):
            att_group = SO2
        elif C_ab.shape == (3, 3):
            att_group = SO3

        if x.direction == "right":
            return x.jacobian_from_blocks(position=x.attitude)
        elif x.direction == "left":
            return x.jacobian_from_blocks(
                attitude=att_group.odot(x.position),
                position=np.identity(x.position.size),
            )

    def covariance(self, x: MatrixLieGroupState) -> np.ndarray:
        return self.R


class Altitude(MeasurementModel):
    def __init__(self, R: np.ndarray):
        self.R = R

    def evaluate(self, x: MatrixLieGroupState):
        return x.position[2]

    def jacobian(self, x: MatrixLieGroupState):

        if x.direction == "right":
            return x.jacobian_from_blocks(position=x.attitude[2, :].reshape((1, -1)))
        elif x.direction == "left":
            return x.jacobian_from_blocks(
                attitude=SO3.odot(x.position)[2, :].reshape((1, -1)),
                position=np.array(([[0, 0, 1]])),
            )

    def covariance(self, x: MatrixLieGroupState) -> np.ndarray:
        return self.R


class Gravity(MeasurementModel):
    def __init__(self, R: np.ndarray, gravity_vec=[0, 0, -9.80665]):
        self.R = R
        self._g_a = np.array(gravity_vec).reshape((-1, 1))

    def evaluate(self, x: MatrixLieGroupState):
        return x.attitude.T @ self._g_a

    def jacobian(self, x: MatrixLieGroupState):
        if x.direction == "right":
            return x.jacobian_from_blocks(attitude=-SO3.odot(x.attitude.T @ self._g_a))
        elif x.direction == "left":

            return x.jacobian_from_blocks(attitude=-SO3.odot(x.attitude.T) @ self._g_a)

    def covariance(self, x: MatrixLieGroupState) -> np.ndarray:
        return self.R